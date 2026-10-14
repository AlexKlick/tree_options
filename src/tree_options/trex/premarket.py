"""Owner-installed research startup only. No broker/order/mandate operations."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

RELEASE = Path(os.environ.get("TREX_RELEASE_DIR", Path(__file__).resolve().parents[3]))
RUNS = Path(os.environ.get("TREX_PREMARKET_RUNS", Path.home() / ".local/state/trex-premarket/runs"))
ET = ZoneInfo("America/New_York")
ALLOCATION_KEY = "operator-paper-sleeves-20261001"


def config():
    return json.loads((RELEASE / "config.json").read_text())


def verify(c):
    from tree_options.trex.release import verify_manifest

    verify_manifest(c)
    return Path(c["source"])


def window(now, c, cal):
    if now.date() < date.fromisoformat(c["first_session"]):
        return "SKIPPED_BEFORE_FIRST_SESSION"
    if not cal.is_session(now.date()):
        return "SKIPPED_NON_SESSION"
    if not time(8) <= now.time().replace(tzinfo=None) < time(9, 30):
        return "SKIPPED_OUTSIDE_PREMARKET_WINDOW"
    return None


def persist(report):
    RUNS.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + report["mode"]
    encoded = json.dumps(report, indent=2) + "\n"
    p = RUNS / (name + ".json")
    p.write_text(encoded)
    os.chmod(p, 0o600)
    latest = RUNS / "latest.json"
    tmp = RUNS / (".latest-" + str(os.getpid()))
    tmp.write_text(encoded)
    os.chmod(tmp, 0o600)
    tmp.replace(latest)
    print(json.dumps({"status": report["status"], "receipt": str(p), "orders_authorized": False}))


def workspace_summary(c):
    from tree_options.trex.paper_workspace import PaperWorkspace

    state = PaperWorkspace(Path(c["paper_workspace"]), catalog=Path(c["paper_catalog"]))
    allocation = state.allocations()["plans"]
    if len(allocation) != 1 or allocation[0]["total_capital_usd"] != "1000000":
        raise ValueError("allocation_not_prepared")
    plan = allocation[0]
    if len(plan["sleeves"]) != 29 or plan["execution_authorized"] is not False:
        raise ValueError("allocation_contract_changed")
    accounts = state.accounts()
    return {
        "allocation": {
            "status": "PASS",
            "sleeves": 29,
            "large_sleeves": 19,
            "small_sleeves": 10,
            "total_capital_usd": "1000000",
            "scope": "virtual funding",
        },
        "broker": {
            "status": "BLOCKED",
            "reason": "external_ibkr_qualification_and_equity_execution_not_enabled",
            "configured_provider_types": sorted({a["provider"] for a in accounts["accounts"]}),
            "account_rows": len(accounts["accounts"]),
            "blockers": sorted(
                set(
                    accounts.get("blockers", [])
                    + [code for a in accounts["accounts"] for code in a.get("blockers", [])]
                )
            ),
            "orders_authorized": False,
        },
    }


def api(path):
    with urllib.request.urlopen("http://127.0.0.1:8090" + path, timeout=5) as r:
        return json.load(r)


def shadow_input_check(c, source):
    result = subprocess.run(
        [
            str(source / ".venv/bin/python"),
            "-m",
            "tree_options.desk",
            "qsl",
            "--dry-run",
        ],
        cwd=source,
        capture_output=True,
        text=True,
        timeout=180,
    )
    RUNS.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = RUNS / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ") + "-qsl-input-check.log")
    log.write_text(result.stdout + result.stderr)
    os.chmod(log, 0o600)
    return {
        "status": "PASS"
        if result.returncode == 0
        else "BLOCKED"
        if result.returncode == 3
        else "FAIL",
        "exit": result.returncode,
        "log": str(log),
        "scope": "recorded-chain shadow input check; no queue write or external fill proof",
    }


def activate_workspace(source):
    # Existing web owner is reused. Never invoke the supervised broker owner.
    active = (
        subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", "trex-web.service"]
        ).returncode
        == 0
    )
    pid = subprocess.check_output(
        ["systemctl", "--user", "show", "trex-web.service", "-p", "MainPID", "--value"], text=True
    ).strip()
    same = False
    if active and pid.isdigit() and pid != "0":
        try:
            same = Path("/proc", pid, "cwd").resolve() == source.resolve()
        except OSError:
            pass
    if not same:
        raise ValueError("web_release_not_active")
    datasets, allocations, accounts, jobs = [
        api(p)
        for p in (
            "/api/research/quant/datasets",
            "/api/paper/allocations",
            "/api/paper/accounts",
            "/api/research/quant/jobs",
        )
    ]
    if (
        datasets["controls_enabled"] is not True
        or datasets["execution_authorized"] is not False
        or datasets["live_money"] is not False
    ):
        raise ValueError("workspace_api_contract_changed")
    if len(allocations["plans"]) != 1 or allocations["plans"][0]["total_capital_usd"] != "1000000":
        raise ValueError("workspace_api_allocation_missing")
    if any(a["tradeable"] or a["equity_execution_ready"] for a in accounts["accounts"]):
        raise ValueError("unexpected_equity_authority")
    subprocess.run(["systemctl", "--user", "start", "desk-qsl.timer"], check=True, timeout=30)
    return {
        "status": "PASS",
        "existing_web_owner": "reused" if same else "loaded_pinned_release",
        "dataset_count": len(datasets["datasets"]),
        "stored_job_count": len(jobs["jobs"]),
        "qsl_timer": "started",
        "broker_owner_started": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--prepare", action="store_true")
    modes.add_argument("--preview", action="store_true")
    modes.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    c = config()
    mode = (
        "check"
        if args.check
        else "prepare"
        if args.prepare
        else "preview"
        if args.preview
        else "run"
    )
    report = {
        "mode": mode,
        "source_head": c["release_head"],
        "assessed_at": datetime.now(UTC).isoformat(),
        "orders_authorized": False,
        "live_money": False,
        "status": "FAILED",
    }
    try:
        source = verify(c)
        if args.check:
            print(
                json.dumps(
                    {"status": "PASS", "source_head": c["release_head"], "orders_authorized": False}
                )
            )
            return 0
        from tree_options.trex.clock import session_calendar

        cal = session_calendar()
        if args.prepare:
            from tree_options.trex.paper_workspace import PaperWorkspace

            state = PaperWorkspace(Path(c["paper_workspace"]), catalog=Path(c["paper_catalog"]))
            if not state.allocations()["plans"]:
                state.create_allocation(ALLOCATION_KEY, account_alias=None)
        if args.run:
            skip = window(datetime.now(ET), c, cal)
            if skip:
                report["status"] = skip
                persist(report)
                return 0
        report.update(workspace_summary(c))
        if args.run or args.preview:
            report["shadow_inputs"] = shadow_input_check(c, source)
        if args.run:
            report["startup"] = activate_workspace(source)
            report["status"] = (
                "RESEARCH_STARTED_BROKER_BLOCKED"
                if report["shadow_inputs"]["status"] == "PASS"
                else "RESEARCH_INPUTS_BLOCKED"
            )
        else:
            report["first_session_is_nyse_session"] = cal.is_session(
                date.fromisoformat(c["first_session"])
            )
            report["status"] = "PREPARED" if args.prepare else "PREVIEW_ONLY"
            report["startup"] = {
                "status": "N/A",
                "reason": "no_service_changes_in_preparation_or_preview",
            }
        persist(report)
        return 3 if report.get("shadow_inputs", {}).get("status") in ("FAIL", "BLOCKED") else 0
    except Exception as error:
        report["error_class"] = type(error).__name__
        report["reason"] = str(error) if isinstance(error, ValueError) else "startup_failed"
        persist(report)
        return 1


if __name__ == "__main__":
    sys.exit(main())
