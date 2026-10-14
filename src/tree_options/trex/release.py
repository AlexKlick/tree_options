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
]
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


def _configuration(config: dict[str, Any]) -> Path:
    source = Path(config["source"])
    if not source.is_absolute() or source.resolve() != source:
        raise ValueError("release_source_not_canonical")
    if config.get("orders_authorized") is not False or config.get("live_money") is not False:
        raise ValueError("research_only_contract_changed")
    for key in STATE_KEYS:
        path = Path(config[key])
        if not path.is_absolute() or path.resolve().is_relative_to(source):
            raise ValueError("release_state_inside_source_or_relative")
        if any(c in str(path) for c in ("\n", "\r", '"', "%", "$", " ")):
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
    if config.get("previous_release") is not None:
        previous = Path(config["previous_release"])
        if not previous.is_absolute() or not previous.is_file():
            raise ValueError("previous_release_manifest_missing")
    if _git(source, "status", "--porcelain"):
        raise ValueError("release_source_changed")
    return source


def seal(config: dict[str, Any]) -> dict[str, Any]:
    source = _configuration(config)
    if any(c in str(source) for c in ("\n", "\r", '"', "%", "$", " ")):
        raise ValueError("release_source_path_unsafe_for_unit")
    return {
        **config,
        "schema": SCHEMA,
        "release_head": _git(source, "rev-parse", "HEAD"),
        "release_tree": _git(source, "rev-parse", "HEAD^{tree}"),
        "experiment_sources_sha256": _hash(Path(config["experiment_sources"])),
        "uv_lock_sha256": _hash(source / "uv.lock"),
        "frontend_lock_sha256": _hash(source / "web/package-lock.json"),
        "static_sha256": _assets(source),
        "environment": _environment(source),
        "supported_commands": COMMANDS,
    }


def verify_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema") != SCHEMA:
        raise ValueError("release_schema_invalid")
    expected = seal(manifest)
    for key in (
        "release_head",
        "release_tree",
        "experiment_sources_sha256",
        "uv_lock_sha256",
        "frontend_lock_sha256",
        "static_sha256",
        "environment",
        "supported_commands",
    ):
        if manifest.get(key) != expected[key]:
            raise ValueError(f"release_identity_changed:{key}")


def render_units(manifest: dict[str, Any], destination: Path) -> None:
    verify_manifest(manifest)
    source = Path(manifest["source"])
    if destination.exists():
        raise ValueError("unit_destination_exists")
    destination.mkdir(parents=True)
    values = {**manifest, "release_dir": str(destination.parent)}
    for template in sorted((source / "deploy/product").glob("*.in")):
        content = template.read_text()
        for key, value in values.items():
            content = content.replace("@" + key.upper() + "@", str(value))
        if "@" in content:
            raise ValueError("unresolved_unit_template")
        (destination / template.name.removesuffix(".in")).write_text(content)


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
