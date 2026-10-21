"""Read-only inventory of every trex-*/desk-* user unit and the code it runs.

Maps each unit to its source (the serving checkout, a release <sha>, or an
untracked copy), lists timers with their next fire, the state roots the units
name, registered worktrees, unpushed branches and release directories (whether
any unit references them). Emits JSON (schema trex-inventory/1) with --json or
a short text table; --out also writes the JSON document to that one path.

Read-only by construction: every probe passes `check_read_only` (systemctl
list/show verbs, three fixed git read shapes with --no-optional-locks) before it
runs. It never starts, stops or reloads units, never fetches, and never writes
anywhere except --out. Environment values are never recorded except absolute
paths under non-secret names; environment files are listed by name only.

    HOME=/home/alexk .venv/bin/python -B scripts/trex_inventory.py [--json] [--out PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "trex-inventory/1"
PATTERNS = ("trex-*", "desk-*")
UPSTREAM = "origin/main"
BRANCH_FORMAT = "--format=%(refname:short)%09%(upstream)%09%(objectname)"
SERVICE_PROPS = (
    "Id,Description,LoadState,ActiveState,SubState,UnitFileState,FragmentPath,DropInPaths,"
    "WorkingDirectory,Environment,EnvironmentFiles,ExecStartPre,ExecStart,ExecStartPost,"
    "ReadWritePaths,TriggeredBy"
)
TIMER_PROPS = "Id,Unit,ActiveState,UnitFileState,TimersCalendar,TimersMonotonic"
EXEC_PHASES = ("ExecStartPre", "ExecStart", "ExecStartPost")
SYSTEMCTL_VERBS = frozenset({"list-unit-files", "list-units", "list-timers", "show"})
NOT_STATE = frozenset({"PYTHONPATH", "HOME", "PATH", "VIRTUAL_ENV", "PWD", "TMPDIR"})
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSW|API_?KEY|CREDENTIAL|PRIVATE|AUTH|COOKIE|_KEY$", re.I)

Runner = Callable[[Sequence[str]], tuple[int, str]]


@dataclass(frozen=True)
class Paths:
    serving_checkout: Path
    release_parents: tuple[Path, ...]
    release_dirs: tuple[Path, ...]
    templates: Path


def default_paths(home: Path) -> Paths:
    return Paths(
        serving_checkout=home / "documents/tree_options",
        release_parents=(
            home / ".local/state/trex-premarket/releases",
            home / ".local/share/trex/releases",
        ),
        release_dirs=(home / ".local/state/trex-staging/release",),
        templates=Path(__file__).resolve().parents[1] / "deploy/product",
    )


# --- read-only probes ------------------------------------------------------


def check_read_only(argv: Sequence[str]) -> None:
    """Refuse anything but the fixed read-only probe shapes this script issues."""
    a = list(argv)
    ok = False
    if a[:2] == ["systemctl", "--user"] and len(a) > 2:
        ok = a[2] in SYSTEMCTL_VERBS
    elif a[:3] == ["git", "--no-optional-locks", "-C"] and len(a) > 4:
        rest = a[4:]
        ok = (
            rest == ["worktree", "list", "--porcelain"]
            or rest == ["rev-parse", "HEAD"]
            or (
                len(rest) == 4
                and rest[:2] == ["branch", "--no-merged"]
                and not rest[2].startswith("-")
                and rest[3] == BRANCH_FORMAT
            )
        )
    if not ok:
        raise ValueError(f"inventory_command_not_read_only:{' '.join(a[:6])}")


def run_read_only(argv: Sequence[str]) -> tuple[int, str]:
    check_read_only(argv)
    env = {**os.environ, "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return proc.returncode, proc.stdout


class _Probe:
    def __init__(self, runner: Runner):
        self.runner = runner
        self.errors: list[dict[str, Any]] = []

    def __call__(self, argv: list[str]) -> str | None:
        check_read_only(argv)
        code, out = self.runner(argv)
        if code != 0:
            label = "git " + argv[4] if argv[0] == "git" else " ".join(argv[:3])
            self.errors.append({"command": label, "returncode": code})
            return None
        return out


# --- parsing -----------------------------------------------------------------


def _blocks(text: str | None) -> list[dict[str, list[str]]]:
    """`systemctl show` output for several units: blank-line separated blocks."""
    out: list[dict[str, list[str]]] = []
    for chunk in (text or "").split("\n\n"):
        props: dict[str, list[str]] = {}
        for line in chunk.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                props.setdefault(key, []).append(value)
        if props.get("Id"):
            out.append(props)
    return out


def _one(props: dict[str, list[str]], key: str) -> str:
    values = [v for v in props.get(key, []) if v]
    return values[0] if values else ""


def _words(props: dict[str, list[str]], key: str) -> list[str]:
    return [w for v in props.get(key, []) for w in v.split()]


def _split(value: str) -> list[str]:
    try:
        return shlex.split(value)
    except ValueError:
        return value.split()


def _argv(value: str) -> list[str]:
    match = re.search(r"argv\[\]=(.*?) ; ignore_errors=", value)
    return _split(match.group(1)) if match else []


def _strip_prefix(path: str) -> str:
    return path.lstrip("-+!:@")


def _norm(path: str | Path) -> str:
    return os.path.normpath(str(path))


def _inside(path: str, root: str | Path) -> bool:
    return Path(path).is_relative_to(Path(root))


def _utc(us: Any) -> str | None:
    if not isinstance(us, int) or us <= 0:
        return None
    return datetime.fromtimestamp(us // 1_000_000, tz=UTC).isoformat()


# --- unit model -----------------------------------------------------------------


def _environment(props: dict[str, list[str]]) -> dict[str, str]:
    env: dict[str, str] = {}
    for value in props.get("Environment", []):
        for item in _split(value):
            key, sep, val = item.partition("=")
            if sep:
                env[key] = val
    return env


def _interpreter_root(argv: list[str]) -> str | None:
    for token in argv:
        if "/.venv/bin/" in token:
            return _norm(token.split("/.venv/bin/")[0])
    return None


def _entry(argv: list[str]) -> str | None:
    """`-m module [sub]`, or the script a python interpreter runs."""
    if "-m" in argv[:-1]:
        rest = argv[argv.index("-m") + 1 :]
        sub = rest[1:2] if len(rest) > 1 and not rest[1].startswith("-") else []
        return " ".join(rest[:1] + sub)
    for i, token in enumerate(argv[:-1]):
        if "/bin/python" in token and argv[i + 1].endswith(".py"):
            return argv[i + 1]
    return None


def _service(props: dict[str, list[str]]) -> dict[str, Any]:
    env = _environment(props)
    execs = []
    for phase in EXEC_PHASES:
        for value in props.get(phase, []):
            argv = _argv(value)
            if argv:
                execs.append({"phase": phase, "program": argv[0], "entry": _entry(argv)})
    pythonpath = env.get("PYTHONPATH", "").split(":")[0] or None
    py_root = None
    if pythonpath:
        py_root = _norm(pythonpath.removesuffix("/src") if pythonpath != "/src" else pythonpath)
    workdir = _strip_prefix(_one(props, "WorkingDirectory"))
    working = _norm(workdir) if workdir else None
    exec_argv = [_argv(v) for phase in EXEC_PHASES for v in props.get(phase, [])]
    start = [_argv(v) for v in props.get("ExecStart", [])]
    interp = next((r for r in map(_interpreter_root, start) if r), None)
    roots = [r for r in (py_root, interp, working) if r]
    basis = (
        "PYTHONPATH"
        if py_root
        else "interpreter"
        if interp
        else "WorkingDirectory"
        if working
        else None
    )
    referenced = set(roots)
    referenced |= {_norm(t) for argv in exec_argv for t in argv if t.startswith("/")}
    state = {
        k: _norm(v)
        for k, v in sorted(env.items())
        if v.startswith("/") and k not in NOT_STATE and not SECRET_NAME.search(k)
    }
    return {
        "unit": _one(props, "Id"),
        "description": _one(props, "Description"),
        "load_state": _one(props, "LoadState"),
        "active_state": _one(props, "ActiveState"),
        "sub_state": _one(props, "SubState"),
        "unit_file_state": _one(props, "UnitFileState") or None,
        "fragment_path": _one(props, "FragmentPath") or None,
        "drop_ins": _words(props, "DropInPaths"),
        "environment_files": [v.split(" (")[0] for v in props.get("EnvironmentFiles", []) if v],
        "working_directory": working,
        "pythonpath": pythonpath,
        "exec": execs,
        "source": {
            "root": roots[0] if roots else None,
            "basis": basis,
            "consistent": len(set(roots)) <= 1,
        },
        "state_paths": state,
        "read_write_paths": [_norm(_strip_prefix(p)) for p in _words(props, "ReadWritePaths")],
        "triggered_by": _words(props, "TriggeredBy"),
        "_referenced": sorted(referenced),
    }


def _template_coverage(templates: Path) -> dict[str, str]:
    coverage: dict[str, str] = {}
    if not templates.is_dir():
        return coverage
    for template in sorted(templates.glob("*.in")):
        name = template.name.removesuffix(".in")
        if name.endswith("-release.conf"):
            coverage[name.removesuffix("-release.conf") + ".service"] = "dropin"
        elif name.endswith((".service", ".timer")):
            coverage.setdefault(name, "unit")
    return coverage


def _worktrees(text: str | None) -> list[dict[str, Any]]:
    trees = []
    for chunk in (text or "").split("\n\n"):
        tree: dict[str, Any] = {}
        for line in chunk.splitlines():
            key, _, value = line.partition(" ")
            if key == "worktree":
                tree["path"] = _norm(value)
            elif key == "HEAD":
                tree["head"] = value
            elif key == "branch":
                tree["branch"] = value.removeprefix("refs/heads/")
            elif key in ("detached", "prunable", "locked", "bare"):
                tree[key] = True
        if tree.get("path"):
            trees.append(
                {
                    "path": tree["path"],
                    "head": tree.get("head"),
                    "branch": tree.get("branch"),
                    "detached": bool(tree.get("detached")),
                    "prunable": bool(tree.get("prunable")),
                    "locked": bool(tree.get("locked")),
                    "exists": Path(tree["path"]).exists(),
                }
            )
    return trees


# --- inventory ---------------------------------------------------------------------


def build_inventory(paths: Paths, runner: Runner, now: datetime | None = None) -> dict[str, Any]:
    probe = _Probe(runner)
    serving = _norm(paths.serving_checkout)
    sysd = ["systemctl", "--user"]
    quiet = ["--no-legend", "--plain", "--no-pager"]
    names: set[str] = set()
    for listing in (["list-unit-files"], ["list-units", "--all"]):
        out = probe([*sysd, *listing, *quiet, *PATTERNS])
        names |= {line.split()[0] for line in (out or "").splitlines() if line.split()}
    names = {n for n in names if n.startswith(("trex-", "desk-"))}
    services = sorted(n for n in names if n.endswith(".service") and "@." not in n)
    timer_names = sorted(n for n in names if n.endswith(".timer") and "@." not in n)

    git = ["git", "--no-optional-locks", "-C"]
    trees = _worktrees(probe([*git, serving, "worktree", "list", "--porcelain"]))
    heads = {t["path"]: t for t in trees}

    candidates: list[str] = []
    for parent in paths.release_parents:
        if parent.is_dir():
            candidates += [_norm(c) for c in sorted(parent.iterdir()) if c.is_dir()]
    candidates += [_norm(d) for d in paths.release_dirs if d.is_dir()]

    def release_of(path: str) -> str | None:
        return next((c for c in candidates if _inside(path, c)), None)

    rev_cache: dict[str, str | None] = {}

    def identity(root: str) -> tuple[bool, str | None, str | None]:
        """(tracked, head, branch); git only runs where the root owns a .git."""
        if not (Path(root) / ".git").exists():
            return False, None, None
        if root in heads:
            return True, heads[root]["head"], heads[root]["branch"]
        if root not in rev_cache:
            out = probe([*git, root, "rev-parse", "HEAD"])
            rev_cache[root] = out.strip() if out else None
        return True, rev_cache[root], None

    def show(fields: str, targets: list[str]) -> list[dict[str, list[str]]]:
        # With no unit names `systemctl show` describes the manager itself.
        if not targets:
            return []
        return _blocks(probe([*sysd, "show", "--no-pager", "-p", fields, *targets]))

    coverage = _template_coverage(paths.templates)
    units: list[dict[str, Any]] = []
    for props in show(SERVICE_PROPS, services):
        unit = _service(props)
        source = unit["source"]
        root = source["root"]
        head: str | None = None
        branch: str | None = None
        if root is None:
            kind = "unknown"
        elif root == serving:
            kind = "serving_checkout"
            head, branch = heads.get(serving, {}).get("head"), heads.get(serving, {}).get("branch")
        elif not Path(root).exists():
            kind = "missing"
        else:
            tracked, head, branch = identity(root)
            kind = "release" if tracked else "untracked_copy"
        source.update(kind=kind, head=head, branch=branch)
        unit["release_template"] = coverage.get(unit["unit"])
        units.append(unit)
    seen = {u["unit"] for u in units}
    for missing in sorted(set(services) - seen):
        units.append({"unit": missing, "load_state": "unshown", "source": {"kind": "unknown"}})

    listed = probe([*sysd, "list-timers", "--all", "--no-pager", "-o", "json", *PATTERNS])
    try:
        fires = {t["unit"]: t for t in json.loads(listed or "[]") if isinstance(t, dict)}
    except json.JSONDecodeError:
        probe.errors.append({"command": "systemctl --user list-timers", "returncode": "bad_json"})
        fires = {}
    by_unit = {u["unit"]: u for u in units}
    timers = []
    for props in show(TIMER_PROPS, timer_names):
        name = _one(props, "Id")
        target = _one(props, "Unit")
        fire = fires.get(name, {})
        calendar = [
            m.group(1)
            for v in props.get("TimersCalendar", [])
            if (m := re.search(r"OnCalendar=(.*?) ; ", v))
        ]
        monotonic = [
            m.group(1)
            for v in props.get("TimersMonotonic", [])
            if (m := re.search(r"\{ (\S+=\S+) ; ", v))
        ]
        timers.append(
            {
                "timer": name,
                "activates": target,
                "active_state": _one(props, "ActiveState"),
                "unit_file_state": _one(props, "UnitFileState") or None,
                "next_fire_utc": _utc(fire.get("next")),
                "last_fire_utc": _utc(fire.get("last")),
                "calendar": calendar,
                "monotonic": monotonic,
                "target_source": by_unit.get(target, {}).get("source", {}).get("kind"),
            }
        )
    for unit in units:
        unit["timers"] = sorted(t["timer"] for t in timers if t["activates"] == unit["unit"])
        nexts = [t["next_fire_utc"] for t in timers if t["timer"] in unit["timers"]]
        unit["next_fire_utc"] = min((n for n in nexts if n), default=None)

    # Release directories: scanned candidates plus any non-serving unit root.
    for unit in units:
        root = unit["source"].get("root")
        if root and root != serving and release_of(root) is None and Path(root).exists():
            candidates.append(root)
    release_dirs = []
    for path in sorted(set(candidates)):
        source_root = str(Path(path) / "source") if (Path(path) / "source").is_dir() else path
        tracked, head, _ = identity(source_root)
        refs = sorted(
            u["unit"] for u in units if any(_inside(r, path) for r in u.get("_referenced", []))
        )
        release_dirs.append(
            {
                "path": path,
                "source_root": source_root,
                "tracked": tracked,
                "head": head,
                "referenced_by": refs,
                "referenced": bool(refs),
            }
        )

    for tree in trees:
        path = tree["path"]
        tree["role"] = (
            "serving_checkout" if path == serving else "release" if release_of(path) else "worktree"
        )

    branches = probe([*git, serving, "branch", "--no-merged", UPSTREAM, BRANCH_FORMAT])
    unpushed = []
    for line in (branches or "").splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] and not parts[1]:
            unpushed.append({"branch": parts[0], "head": parts[2]})

    roots: dict[str, dict[str, set[str]]] = {}
    for unit in units:
        named = [*unit.get("state_paths", {}).items()]
        named += [("ReadWritePaths", p) for p in unit.get("read_write_paths", [])]
        for key, path in named:
            entry = roots.setdefault(path, {"units": set(), "keys": set()})
            entry["units"].add(unit["unit"])
            entry["keys"].add(key)
    state_roots = [
        {
            "path": path,
            "exists": Path(path).exists(),
            "inside_serving_checkout": _inside(path, serving),
            "units": sorted(entry["units"]),
            "keys": sorted(entry["keys"]),
        }
        for path, entry in sorted(roots.items())
    ]

    for unit in units:
        unit.pop("_referenced", None)
    kinds: dict[str, int] = {}
    for unit in units:
        kind = unit["source"]["kind"]
        kinds[kind] = kinds.get(kind, 0) + 1
    in_use = {
        release_of(u["source"]["root"]) or u["source"]["root"]
        for u in units
        if u["source"]["kind"] == "untracked_copy"
    }
    return {
        "schema": SCHEMA,
        "generated_at": (now or datetime.now(UTC)).isoformat(),
        "read_only": True,
        "serving_checkout": serving,
        "compared_to": UPSTREAM,
        "units": units,
        "timers": timers,
        "state_roots": state_roots,
        "worktrees": trees,
        "unpushed_branches": unpushed,
        "release_dirs": release_dirs,
        "summary": {
            "units": len(units),
            "timers": len(timers),
            "by_source_kind": dict(sorted(kinds.items())),
            "inconsistent_units": sorted(
                u["unit"] for u in units if u["source"].get("consistent") is False
            ),
            "untracked_copies_in_use": sorted(p for p in in_use if p),
            "release_dirs": len(release_dirs),
            "unreferenced_release_dirs": [d["path"] for d in release_dirs if not d["referenced"]],
            "worktrees": len(trees),
            "unpushed_branches": len(unpushed),
        },
        "errors": probe.errors,
    }


def render_text(doc: dict[str, Any]) -> str:
    rows = [("UNIT", "STATE", "SOURCE", "REF", "NEXT FIRE (UTC)")]
    for unit in doc["units"]:
        source = unit["source"]
        root = source.get("root")
        if source.get("head"):
            ref = source["head"][:12] + (f" {source['branch']}" if source.get("branch") else "")
        else:
            ref = Path(root).name if root else "-"
        rows.append(
            (
                unit["unit"],
                unit.get("active_state") or unit.get("load_state") or "-",
                source["kind"] + ("" if source.get("consistent", True) else "!"),
                ref,
                unit.get("next_fire_utc") or "-",
            )
        )
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    lines = [f"TREX inventory ({doc['schema']}) generated {doc['generated_at']}"]
    lines += [
        "  ".join([*(c.ljust(w) for c, w in zip(r[:4], widths, strict=True)), r[4]]) for r in rows
    ]
    s = doc["summary"]
    kinds = " ".join(f"{k}={v}" for k, v in s["by_source_kind"].items())
    lines.append(
        f"units={s['units']} ({kinds}) | timers={s['timers']} | release dirs={s['release_dirs']}"
        f" (unreferenced {len(s['unreferenced_release_dirs'])}) | worktrees={s['worktrees']}"
        f" | unpushed branches={s['unpushed_branches']} | errors={len(doc['errors'])}"
    )
    if s["untracked_copies_in_use"]:
        lines.append("untracked copies in use: " + " ".join(s["untracked_copies_in_use"]))
    if s["inconsistent_units"]:
        lines.append("mixed sources (!): " + " ".join(s["inconsistent_units"]))
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None, runner: Runner | None = None) -> int:
    defaults = default_paths(Path.home())
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", action="store_true", help="print JSON instead of the table")
    parser.add_argument("--out", type=Path, help="also write the JSON document here")
    parser.add_argument("--serving-checkout", type=Path, default=defaults.serving_checkout)
    parser.add_argument("--release-parent", type=Path, action="append")
    parser.add_argument("--release-dir", type=Path, action="append")
    parser.add_argument("--templates", type=Path, default=defaults.templates)
    args = parser.parse_args(argv)
    paths = Paths(
        serving_checkout=args.serving_checkout,
        release_parents=tuple(args.release_parent or defaults.release_parents),
        release_dirs=tuple(args.release_dir or defaults.release_dirs),
        templates=args.templates,
    )
    if args.out is not None:
        out = Path(os.path.abspath(args.out))
        protected = [paths.serving_checkout, *paths.release_parents, *paths.release_dirs]
        if any(out.is_relative_to(Path(os.path.abspath(p))) for p in protected):
            print(f"refusing --out inside a code tree: {out}", file=sys.stderr)
            return 2
    doc = build_inventory(paths, runner or run_read_only)
    text = json.dumps(doc, indent=2) + "\n"
    if args.out is not None:
        # Host paths and unit layout only, but still private to the owner.
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
    sys.stdout.write(text if args.json else render_text(doc))
    return 3 if doc["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
