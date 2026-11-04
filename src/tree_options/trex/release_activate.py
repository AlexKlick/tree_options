"""Install one sealed release's rendered units, prove them, or put every byte back.

``prepare`` verifies the release (its identity, an exit-0 gate receipt for its
head, and that ``units/`` is exactly what the renderer produces now) and saves
every unit file it will replace or retire. ``apply`` repeats every check under
a lock, installs the buffers it hashed, reloads, proves the effective units and
the restarted services, and writes its receipt; any failure, the receipt
included, restores the saved bytes. ``rollback`` restores them by hand later.

It never touches a broker owner: trex-desk, trex-monitor and the gateway
watchers are not release-pinned and are refused as targets. A release is
artifact identity, not trading authority; activation grants none.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tree_options.trex import release

SCHEMA = "trex-release-activation/1"
#: sorts after every hand-written drop-in, so the release has the last word
DROPIN = "zz-trex-release.conf"
#: earlier release drop-ins this one replaces (each saved before removal)
RETIRED_PREFIXES = ("95-release.conf", "zz-recovery-", DROPIN)
BROKER_UNITS = frozenset(
    {
        "trex-desk.service",
        "trex-monitor.service",
        "trex-gateway-watch.service",
        "trex-exit-watch.service",
    }
)
SHOW_KEYS = (
    "LoadState",
    "FragmentPath",
    "DropInPaths",
    "WorkingDirectory",
    "ExecStartPre",
    "ExecStart",
    "Environment",
    "ReadWritePaths",
    "ProtectHome",
    "ProtectSystem",
    "MainPID",
)


class ActivationRefused(RuntimeError):
    """A check failed; nothing was installed, or everything was put back."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Host:
    """The system calls activation makes (injected by tests)."""

    config_root: Path
    systemctl: Callable[[Sequence[str]], str]
    environ_of: Callable[[int], dict[str, str]]
    http: Callable[[str, str], tuple[int, bytes]]  # status 0: nothing listening yet
    sleep: Callable[[float], None] = time.sleep


def _systemctl(args: Sequence[str]) -> str:
    done = subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, timeout=120
    )
    if done.returncode:
        raise ActivationRefused(f"systemctl_failed:{args[0]}:{done.returncode}")
    return done.stdout


def _environ_of(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    pairs = (item.decode(errors="replace").partition("=") for item in raw.split(b"\0") if item)
    return {key: value for key, _, value in pairs}


def _http(method: str, url: str) -> tuple[int, bytes]:
    request = urllib.request.Request(url, method=method, data=b"{}" if method == "POST" else None)
    request.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, OSError):
        return 0, b""  # refused or reset: the restarted server is not listening yet


def real_host() -> Host:
    return Host(
        config_root=Path.home() / ".config/systemd/user",
        systemctl=_systemctl,
        environ_of=_environ_of,
        http=_http,
    )


# ------------------------------------------------------------------- planning


@dataclass(frozen=True)
class Operation:
    """One file activation writes (``install``) or removes (``retire``)."""

    kind: str  # "install" | "retire"
    unit: str
    path: Path
    before_sha256: str | None  # None: the file did not exist
    after: bytes = field(repr=False, default=b"")

    def document(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "unit": self.unit,
            "path": str(self.path),
            "before_sha256": self.before_sha256,
            "after_sha256": _sha(self.after) if self.kind == "install" else None,
        }


@dataclass(frozen=True)
class Plan:
    head: str
    release_dir: Path
    operations: tuple[Operation, ...]
    dropin_dirs: dict[str, dict[str, str]]  # unit -> {name: sha} on disk
    rendered: dict[str, bytes] = field(repr=False)

    @property
    def units(self) -> list[str]:
        return sorted({op.unit for op in self.operations})

    def document(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "head": self.head,
            "release_dir": str(self.release_dir),
            "operations": [op.document() for op in self.operations],
            "dropin_dirs": self.dropin_dirs,
        }


def _receipt(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        raise ActivationRefused("gate_receipt_unreadable") from None
    return _pairs(lines)


def _pairs(lines: Sequence[str]) -> dict[str, str]:
    """``key=value`` lines (the first ``=`` splits) as a mapping."""
    return {key: value for key, sep, value in (line.partition("=") for line in lines) if sep}


def _fresh_render(manifest: dict[str, Any], release_dir: Path) -> dict[str, bytes]:
    """What the renderer produces for this manifest now, in this release dir."""
    scratch = release_dir / f".verify-units-{os.getpid()}"
    try:
        release.render_units(manifest, scratch)
        return {p.name: p.read_bytes() for p in sorted(scratch.iterdir())}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _target(host: Host, name: str) -> tuple[str, Path]:
    if name.endswith("-release.conf"):
        unit = name.removesuffix("-release.conf") + ".service"
        return unit, host.config_root / f"{unit}.d" / DROPIN
    return name, host.config_root / name


def _dropins(host: Host, unit: str) -> dict[str, str]:
    folder = host.config_root / f"{unit}.d"
    if not folder.is_dir():
        return {}
    found = {}
    for path in sorted(folder.iterdir()):
        if path.is_symlink() or not path.is_file():
            raise ActivationRefused(f"unit_file_not_regular:{path}")
        found[path.name] = _sha(path.read_bytes())
    return found


def _before(path: Path) -> str | None:
    if path.is_symlink():
        raise ActivationRefused(f"unit_file_not_regular:{path}")
    return _sha(path.read_bytes()) if path.exists() else None


def plan(
    host: Host, release_dir: Path, gate_receipt: Path, names: Sequence[str] | None = None
) -> Plan:
    try:
        manifest = json.loads((release_dir / "config.json").read_text())
        release.verify_manifest(manifest)
    except (OSError, ValueError) as error:
        raise ActivationRefused(f"release_unverified:{error}") from None
    head = manifest["release_head"]
    receipt = _receipt(gate_receipt)
    if receipt.get("head") != head or receipt.get("exit") != "0":
        raise ActivationRefused("gate_not_passed_for_release_head")
    rendered = _fresh_render(manifest, release_dir)
    sealed = {p.name: p.read_bytes() for p in sorted((release_dir / "units").iterdir())}
    if sealed != rendered:
        raise ActivationRefused("rendered_units_drifted")
    chosen = sorted(rendered) if names is None else list(names)
    unknown = sorted(set(chosen) - set(rendered))
    if unknown:
        raise ActivationRefused(f"not_a_rendered_unit:{unknown[0]}")
    operations: list[Operation] = []
    dropin_dirs: dict[str, dict[str, str]] = {}
    for name in chosen:
        unit, target = _target(host, name)
        if unit in BROKER_UNITS:
            raise ActivationRefused(f"broker_unit_refused:{unit}")
        if target.name == DROPIN:
            listing = _dropins(host, unit)
            dropin_dirs[unit] = listing
            for old in sorted(listing):
                if old.startswith(RETIRED_PREFIXES) and old != DROPIN:
                    operations.append(Operation("retire", unit, target.parent / old, listing[old]))
        operations.append(Operation("install", unit, target, _before(target), rendered[name]))
    return Plan(head, release_dir, tuple(operations), dropin_dirs, rendered)


# ------------------------------------------------------------------- applying


def _saved_dir(evidence: Path, head: str) -> Path:
    return evidence / f"systemd-before-{head[:12]}"


def _write_atomic(path: Path, data: bytes) -> None:
    """A complete, fsynced file swapped in by rename: never a partial target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def prepare(
    host: Host,
    release_dir: Path,
    gate_receipt: Path,
    evidence: Path,
    names: Sequence[str] | None = None,
) -> dict[str, Any]:
    planned = plan(host, release_dir, gate_receipt, names)
    saved = _saved_dir(evidence, planned.head)
    saved.mkdir(mode=0o700, parents=True)  # once per head: never overwrite saved bytes
    for number, op in enumerate(planned.operations):
        if op.before_sha256 is None:
            continue
        data = op.path.read_bytes()
        if _sha(data) != op.before_sha256:
            raise ActivationRefused(f"unit_changed_while_saving:{op.path}")
        copy = saved / f"{number:02d}-{op.unit}-{op.path.name}"
        copy.write_bytes(data)
        os.chmod(copy, 0o600)
    document = {**planned.document(), "prepared_at": _now(), "names": names, "installed": False}
    (evidence / f"activation-{planned.head[:12]}.prepared.json").write_text(
        json.dumps(document, indent=2) + "\n"
    )
    return document


def _show(host: Host, unit: str) -> dict[str, str]:
    out = host.systemctl(["show", unit, *(f"--property={key}" for key in SHOW_KEYS)])
    return _pairs(out.splitlines())


def _assignments(data: bytes, key: str) -> list[str]:
    prefix = key + "="
    return [
        line.strip()[len(prefix) :]
        for line in data.decode().splitlines()
        if line.strip().startswith(prefix)
    ]


def _check_unit(
    host: Host, planned: Plan, unit: str, blocked: Sequence[str], restarted: bool
) -> None:
    shown = _show(host, unit)
    if shown.get("LoadState") != "loaded":
        raise ActivationRefused(f"unit_not_loaded:{unit}")
    for key in ("WorkingDirectory", "ExecStartPre", "ExecStart", "Environment", "ReadWritePaths"):
        if any(root in shown.get(key, "") for root in blocked):
            raise ActivationRefused(f"blocked_storage_in_effective_unit:{unit}:{key}")
    installed = [op for op in planned.operations if op.unit == unit and op.kind == "install"]
    if any(op.path.name == DROPIN for op in installed):
        expected = {
            name
            for name in planned.dropin_dirs.get(unit, {})
            if not name.startswith(RETIRED_PREFIXES)
        } | {DROPIN}
        on_disk = set(_dropins(host, unit))
        loaded = {
            Path(p).name
            for p in shown.get("DropInPaths", "").split()
            if Path(p).parent == host.config_root / f"{unit}.d"
        }
        if on_disk != expected or loaded != expected:
            raise ActivationRefused(f"dropin_set_changed:{unit}")
    pairs = [
        item for op in installed for item in _assignments(op.after, "Environment") if "=" in item
    ]
    effective = set(shown.get("Environment", "").split())
    if not set(pairs) <= effective:
        raise ActivationRefused(f"effective_environment_differs:{unit}")
    if restarted:
        pid = int(shown.get("MainPID", "0") or 0)
        if pid <= 0:
            raise ActivationRefused(f"service_not_running:{unit}")
        running = host.environ_of(pid)
        for item in pairs:
            key, _, value = item.partition("=")
            if running.get(key) != value:  # an EnvironmentFile overrode the release
                raise ActivationRefused(f"process_environment_differs:{unit}:{key}")


#: a restarted web serves after its release gate and app start (seconds)
WEB_READY_ATTEMPTS = 60


def _prove_web(host: Host, unit: str, read_only: bool, port: int) -> None:
    url = f"http://127.0.0.1:{port}/api/runtime-mode"
    status, body = host.http("GET", url)
    for _ in range(WEB_READY_ATTEMPTS - 1):
        if status != 0:  # only "nothing listening yet" is retried
            break
        host.sleep(1.0)
        status, body = host.http("GET", url)
    if status == 0:
        raise ActivationRefused("web_not_ready")
    if status != 200 or json.loads(body).get("recovery_read_only") is not read_only:
        raise ActivationRefused("web_runtime_mode_differs")
    if read_only:
        status, body = host.http("POST", f"http://127.0.0.1:{port}/api/commands")
        if status != 403 or json.loads(body).get("reason") != "recovery_read_only":
            raise ActivationRefused("web_accepts_mutations")
        shown = _show(host, unit)
        if (
            shown.get("ReadWritePaths", "") != ""
            or shown.get("ProtectHome") != "read-only"
            or shown.get("ProtectSystem") != "strict"
        ):
            raise ActivationRefused("web_sandbox_writable")


def _restore(host: Host, planned: Plan, evidence: Path, done: Sequence[Operation]) -> None:
    saved = _saved_dir(evidence, planned.head)
    index = {op: n for n, op in enumerate(planned.operations)}
    for op in reversed(done):
        if op.before_sha256 is None:
            op.path.unlink(missing_ok=True)
            continue
        copy = saved / f"{index[op]:02d}-{op.unit}-{op.path.name}"
        data = copy.read_bytes()
        if _sha(data) != op.before_sha256:
            raise ActivationRefused(f"saved_bytes_corrupt:{copy}")
        _write_atomic(op.path, data)


@contextmanager
def _locked(evidence: Path) -> Iterator[None]:
    with open(evidence / "activate.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ActivationRefused("activation_in_progress") from None
        yield


def apply(
    host: Host,
    release_dir: Path,
    gate_receipt: Path,
    evidence: Path,
    *,
    restart: Sequence[str] = (),
    web_unit: str = "trex-web.service",
    web_port: int = 8090,
) -> dict[str, Any]:
    with _locked(evidence):
        head = json.loads((release_dir / "config.json").read_text())["release_head"]
        prepared = json.loads((evidence / f"activation-{head[:12]}.prepared.json").read_text())
        planned = plan(host, release_dir, gate_receipt, prepared["names"])
        if planned.document()["operations"] != prepared["operations"] or (
            planned.dropin_dirs != prepared["dropin_dirs"]
        ):
            raise ActivationRefused("prepared_state_changed")
        manifest = json.loads((release_dir / "config.json").read_text())
        blocked = manifest.get("blocked_storage_roots", [])
        done: list[Operation] = []
        try:
            for op in planned.operations:
                done.append(op)
                if op.kind == "retire":
                    op.path.unlink()
                else:
                    _write_atomic(op.path, op.after)
            host.systemctl(["daemon-reload"])
            for unit in restart:
                if unit not in planned.units:
                    raise ActivationRefused(f"restart_outside_plan:{unit}")
                host.systemctl(["restart", unit])
            for unit in planned.units:
                _check_unit(host, planned, unit, blocked, unit in restart)
            if web_unit in restart:
                _prove_web(host, web_unit, manifest.get("recovery_read_only") is True, web_port)
            document = {**prepared, "installed": True, "applied_at": _now(), "restart": restart}
            out = evidence / f"activation-{head[:12]}.applied.json"
            out.write_text(json.dumps(document, indent=2) + "\n")
            return document
        except BaseException:
            _restore(host, planned, evidence, done)
            host.systemctl(["daemon-reload"])
            for unit in restart:
                host.systemctl(["restart", unit])
            raise


def rollback(host: Host, release_dir: Path, evidence: Path, *, restart: Sequence[str] = ()) -> None:
    """Put back every byte a completed activation of this release replaced."""
    with _locked(evidence):
        head = json.loads((release_dir / "config.json").read_text())["release_head"]
        prepared = json.loads((evidence / f"activation-{head[:12]}.prepared.json").read_text())
        saved = _saved_dir(evidence, head)
        for number, op in reversed(list(enumerate(prepared["operations"]))):
            path = Path(op["path"])
            if op["before_sha256"] is None:
                path.unlink(missing_ok=True)
                continue
            data = (saved / f"{number:02d}-{op['unit']}-{path.name}").read_bytes()
            if _sha(data) != op["before_sha256"]:
                raise ActivationRefused(f"saved_bytes_corrupt:{path.name}")
            _write_atomic(path, data)
        host.systemctl(["daemon-reload"])
        for unit in restart:
            host.systemctl(["restart", unit])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "apply", "rollback"))
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--gate-receipt", type=Path)
    parser.add_argument("--unit", action="append", dest="names", help="rendered file name")
    parser.add_argument("--restart", action="append", default=[], help="service to restart")
    args = parser.parse_args(argv)
    host = real_host()
    if args.command != "rollback" and args.gate_receipt is None:
        parser.error(f"{args.command} requires --gate-receipt")
    try:
        if args.command == "prepare":
            result = prepare(host, args.release, args.gate_receipt, args.evidence, args.names)
        elif args.command == "apply":
            result = apply(
                host, args.release, args.gate_receipt, args.evidence, restart=args.restart
            )
        else:
            rollback(host, args.release, args.evidence, restart=args.restart)
            result = {"rolled_back": True}
    except ActivationRefused as error:
        print(json.dumps({"refused": str(error)}))
        return 3
    print(json.dumps({key: result.get(key) for key in ("head", "installed", "rolled_back")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
