"""Local TREX MCP release evidence gate; never activates or connects to a broker.

The TREX repository deliberately uses its local gates, not GitHub CI. This
runner makes the MCP slice reproducible and emits an explicit non-release
verdict. `--full` additionally runs the repository's existing M0 gate.
"""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent.parent
TESTS = (
    "tests/unit/test_trex_mcp.py",
    "tests/unit/test_trex_mcp_hardening.py",
    "tests/unit/test_trex_mcp_oauth_transport.py",
    "tests/unit/test_trex_mcp_quotas.py",
    "tests/unit/test_trex_mcp_bearish_compare.py",
    "tests/unit/test_trex_mcp_network_contract.py",
)
FORBIDDEN_IMPORTS = (
    "ib_async", "snaptrade_python_sdk", "snaptrade", "tree_options.trex.",
    "tree_options.desk.", "tree_options.execution.",
)


def unsafe_imports(source: str) -> list[str]:
    """Check the isolated gateway's direct imports, including lazy imports."""
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module] if node.level == 0 else []
        for name in names:
            if any(name == root.removesuffix(".") or name.startswith(root) for root in FORBIDDEN_IMPORTS):
                found.append(f"L{node.lineno}: {name}")
    return found


def static_gate(root: Path) -> list[str]:
    packages = root / "src/tree_options/trex_mcp"
    failures: list[str] = []
    sources = sorted(packages.glob("*.py"))
    if not sources or not (packages / "server.py").exists():
        return ["MCP package missing"]
    for file in sources:
        try:
            faults = unsafe_imports(file.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            failures.append(f"{file.name}: unreadable Python: {type(exc).__name__}")
            continue
        failures.extend(f"{file.name}: forbidden import {fault}" for fault in faults)
    if not all((root / t).is_file() for t in TESTS):
        failures.append("missing one or more required MCP contract tests")
    return failures


def run_command(command: list[str], root: Path) -> dict[str, Any]:
    start = time.monotonic()
    try:
        p = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=1800, check=False)
        return {"command": command, "exit_code": p.returncode,
                "duration_seconds": round(time.monotonic() - start, 3),
                "stdout_tail": p.stdout[-3000:], "stderr_tail": p.stderr[-1500:]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": command, "exit_code": 125,
                "duration_seconds": round(time.monotonic() - start, 3),
                "error": type(exc).__name__}


def gate(root: Path, *, full: bool = False, static_only: bool = False) -> dict[str, Any]:
    errors = static_gate(root)
    evidence: dict[str, Any] = {"schema": "trex-mcp-pilot-gate/1",
        "checks": {"static_broker_boundary": {"passed": not errors, "errors": errors}},
        "full_repo_gate_requested": full, "host_deployment_checked": False,
        "oauth_interoperability_checked": False, "customer_activation_authorized": False}
    if errors:
        evidence["verdict"] = "BLOCKED_STATIC"
        return evidence
    if not static_only:
        jobs = [
            [sys.executable, "-m", "pytest", "-q", *TESTS],
            [sys.executable, "-m", "compileall", "-q", "src/tree_options/trex_mcp"],
        ]
        if full:
            jobs.append(["bash", "scripts/m0_gate.sh"])
        evidence["checks"]["commands"] = [run_command(job, root) for job in jobs]
        if any(item["exit_code"] != 0 for item in evidence["checks"]["commands"]):
            evidence["verdict"] = "BLOCKED_TESTS"
            return evidence
    # Local software checks, even full M0, never attest to broker custody,
    # real OAuth client, legal market-data redistribution or human signoff.
    evidence["verdict"] = "LOCAL_CHECKS_PASS_NOT_CUSTOMER_READY"
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="Run M0 and mutation gate after MCP slice")
    parser.add_argument("--static-only", action="store_true", help="Non-mutating import boundary checks")
    parser.add_argument("--repo", type=Path, default=HERE)
    args = parser.parse_args()
    if args.full and args.static_only:
        parser.error("--full and --static-only are mutually exclusive")
    result = gate(args.repo.resolve(), full=args.full, static_only=args.static_only)
    print(json.dumps(result, sort_keys=True, indent=2))
    return 1 if result["verdict"].startswith("BLOCKED") else 0


if __name__ == "__main__":
    raise SystemExit(main())
