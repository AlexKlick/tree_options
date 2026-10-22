"""Seal and check a private research/web release; never activate a broker owner.

Run after the locked Python installation and frontend build in a clean, frozen
checkout. State lives outside that checkout. A manifest is artifact identity,
not a grant, an acceptance receipt, or proof of broker readiness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

SCHEMA = "trex-research-release/2"
COMMANDS = [
    "tree_options.trex_web",
    "tree_options.desk qsl",
    "tree_options.desk publish-evidence",
    "tree_options.trex.premarket",
    "tree_options.research.experiments",
    "tree_options.desk shadows",
    "tree_options.desk desk-enter",
    "tree_options.trex.acceptance publish",
    "tree_options.trex.deadman",
]
EXEC_KEYS = (
    "ExecCondition",
    "ExecStartPre",
    "ExecStart",
    "ExecStartPost",
    "ExecReload",
    "ExecStop",
    "ExecStopPost",
)
STATE_KEYS = (
    "research_workspace",
    "paper_workspace",
    "paper_catalog",
    "desk_store",
    "desk_state",
    "artifacts",
    "read_snapshot",
    "premarket_runs",
    "experiment_sources",
    "experiment_snapshot",
)


def _git(source: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=source, text=True).strip()


def _detached(source: Path) -> bool:
    """`git symbolic-ref -q HEAD` exits 1 only for a detached HEAD (128: no repo)."""
    result = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"], cwd=source, capture_output=True, text=True
    )
    return result.returncode == 1


def _main_worktree(source: Path) -> Path:
    """The main worktree of the repository `source` belongs to (its common dir's owner)."""
    common = Path(_git(source, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    common = common.resolve()
    return common.parent if common.name == ".git" else common


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assets(source: Path) -> dict[str, str]:
    root = source / "src/tree_options/trex_web/static"
    if not (root / "index.html").is_file():
        raise ValueError("release_assets_missing")
    if any(p.is_symlink() for p in root.rglob("*")):
        raise ValueError("release_asset_symlink")
    return {str(p.relative_to(root)): _hash(p) for p in sorted(root.rglob("*")) if p.is_file()}


def _environment(source: Path) -> dict[str, Any]:
    # Pin imports to the release even if the calling shell has PYTHONPATH set.
    env = {**os.environ, "PYTHONPATH": str(source / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    if (source / ".venv").is_symlink():
        raise ValueError("release_environment_not_private")
    python = source / ".venv/bin/python"
    output = subprocess.check_output(
        [
            str(python),
            "-c",
            """
import importlib.metadata as m, json, pathlib, sys, tree_options
print(json.dumps({'python': sys.version, 'prefix': sys.prefix,
 'import_root': str(pathlib.Path(tree_options.__file__).resolve().parent),
 'packages': sorted((p.metadata['Name'].lower(), p.version) for p in m.distributions())}))
""",
        ],
        cwd=source,
        env=env,
        text=True,
        timeout=30,
    )
    doc = json.loads(output)
    if Path(doc["prefix"]).resolve() != (source / ".venv").resolve():
        raise ValueError("release_environment_not_private")
    if Path(doc["import_root"]) != source / "src/tree_options":
        raise ValueError("release_import_mismatch")
    result = subprocess.run(
        [str(python), "-m", "tree_options.desk", "qsl", "--help"],
        cwd=source,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode or "--dry-run" not in result.stdout:
        raise ValueError("release_qsl_command_missing")
    result = subprocess.run(
        [str(python), "-m", "tree_options.research.experiments", "--help"],
        cwd=source,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode or "--source-release" not in result.stdout:
        raise ValueError("release_experiment_command_missing")
    return dict(doc)


def _unit_unsafe(path: str | Path) -> bool:
    """Characters a unit file would split, expand or misquote."""
    return any(c in str(path) for c in ("\n", "\r", '"', "%", "$", " "))


def _configuration(config: dict[str, Any]) -> Path:
    source = Path(config["source"])
    if not source.is_absolute() or source.resolve() != source:
        raise ValueError("release_source_not_canonical")
    if _unit_unsafe(source):
        raise ValueError("release_source_path_unsafe_for_unit")
    if config.get("orders_authorized") is not False or config.get("live_money") is not False:
        raise ValueError("research_only_contract_changed")
    for key in STATE_KEYS:
        path = Path(config[key])
        if not path.is_absolute() or path.resolve().is_relative_to(source):
            raise ValueError("release_state_inside_source_or_relative")
        if _unit_unsafe(path):
            raise ValueError("release_state_path_unsafe_for_unit")
    if (
        not Path(config["read_snapshot"])
        .resolve()
        .is_relative_to(Path(config["desk_state"]).resolve())
    ):
        raise ValueError("snapshot_outside_publisher_state")
    if (
        not Path(config["experiment_snapshot"])
        .resolve()
        .is_relative_to(Path(config["desk_state"]).resolve())
    ):
        raise ValueError("experiment_snapshot_outside_publisher_state")
    # A release is one pinned commit. A branch checkout moves with the branch, and
    # the main worktree is the serving checkout other work commits into.
    if not _detached(source):
        raise ValueError("release_source_not_detached")
    if source.is_relative_to(_main_worktree(source)):
        raise ValueError("release_source_in_main_worktree")
    if _git(source, "status", "--porcelain"):
        raise ValueError("release_source_changed")
    return source


def _identity(source: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Everything verification recomputes from the source and compares."""
    return {
        "release_head": _git(source, "rev-parse", "HEAD"),
        "release_tree": _git(source, "rev-parse", "HEAD^{tree}"),
        "experiment_sources_sha256": _hash(Path(config["experiment_sources"])),
        "uv_lock_sha256": _hash(source / "uv.lock"),
        "frontend_lock_sha256": _hash(source / "web/package-lock.json"),
        "static_sha256": _assets(source),
        "environment": _environment(source),
        "supported_commands": COMMANDS,
    }


def seal(config: dict[str, Any]) -> dict[str, Any]:
    source = _configuration(config)
    previous_sha256 = None
    if config.get("previous_release") is not None:
        previous = Path(config["previous_release"])
        if not previous.is_absolute() or not previous.is_file():
            raise ValueError("previous_release_manifest_missing")
        # Hashed now so verification never needs the previous release on disk:
        # pruning an old release directory must not fail a live gate.
        previous_sha256 = _hash(previous)
    return {
        **config,
        "schema": SCHEMA,
        **_identity(source, config),
        "previous_release_sha256": previous_sha256,
    }


def _check_previous_release(manifest: dict[str, Any]) -> None:
    previous = manifest.get("previous_release")
    recorded = manifest.get("previous_release_sha256")
    if previous is None:
        valid = recorded is None
    else:
        valid = (
            isinstance(previous, str)
            and Path(previous).is_absolute()
            and isinstance(recorded, str)
            and len(recorded) == 64
            and set(recorded) <= set("0123456789abcdef")
        )
    if not valid:
        raise ValueError("previous_release_hash_invalid")


def verify_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema") != SCHEMA:
        raise ValueError("release_schema_invalid")
    _check_previous_release(manifest)
    expected = _identity(_configuration(manifest), manifest)
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise ValueError(f"release_identity_changed:{key}")


def _unit_commands(content: str) -> list[str]:
    """`module [subcommand]` of every command line in a unit; "" if not `-m`."""
    commands = []
    for line in content.splitlines():
        key, _, value = line.strip().partition("=")
        tokens = value.split()
        if key not in EXEC_KEYS or not tokens:
            continue
        if "-m" not in tokens[:-1]:
            commands.append("")
            continue
        rest = tokens[tokens.index("-m") + 1 :]
        sub = rest[1:2] if len(rest) > 1 and not rest[1].startswith("-") else []
        commands.append(" ".join(rest[:1] + sub))
    return commands


def render_units(manifest: dict[str, Any], destination: Path) -> None:
    """Write unit files and drop-ins for one verified release; never installs them.

    All or nothing: every template is resolved and checked before anything is
    written, so a refused render leaves no partial unit set behind.

    Units name `destination.parent` as TREX_RELEASE_DIR, and their gate verifies
    whatever config.json it finds there, so that file must be this manifest.
    """
    verify_manifest(manifest)
    source = Path(manifest["source"])
    if destination.exists():
        raise ValueError("unit_destination_exists")
    release_dir = destination.parent
    if not destination.is_absolute() or _unit_unsafe(release_dir):
        raise ValueError("release_dir_unsafe_for_unit")
    try:
        sealed = json.loads((release_dir / "config.json").read_text())
    except (OSError, ValueError):
        sealed = None
    if sealed != json.loads(json.dumps(manifest)):
        raise ValueError("release_dir_config_mismatch")
    values = {**manifest, "release_dir": str(release_dir)}
    supported = set(manifest["supported_commands"])
    rendered: dict[str, str] = {}
    for template in sorted((source / "deploy/product").glob("*.in")):
        content = template.read_text()
        for key, value in values.items():
            content = content.replace("@" + key.upper() + "@", str(value))
        if "@" in content:
            raise ValueError("unresolved_unit_template")
        for command in _unit_commands(content):
            if command not in supported:
                raise ValueError(f"release_template_command_unbound:{template.name}:{command}")
        rendered[template.name.removesuffix(".in")] = content
    destination.mkdir(parents=True)
    for name, content in rendered.items():
        (destination / name).write_text(content)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("seal", "verify", "render"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if args.command == "verify":
        verify_manifest(config)
    elif args.command == "render":
        if args.out is None:
            parser.error("render requires --out")
        render_units(config, args.out)
    else:
        if args.out is None:
            parser.error("seal requires --out")
        manifest = seal(config)
        with args.out.open("x") as out:
            out.write(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"status": "ok", "operation": args.command, "orders_authorized": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
