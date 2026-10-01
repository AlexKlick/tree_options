"""Private offline onboarding and read-only broker-paper qualification.

A receipt describes observations during one owned session. It is neither a
mandate nor a continuing ownership lease, and never enables broker effects.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from tree_options.action_graph.proposal import canonical_bytes
from tree_options.execution.snaptrade_provider import (
    SDK_VERSION,
    ProviderUnavailable,
    SnapTradeProvider,
    account_findings,
    load_private_binding,
)
from tree_options.time.sessions import shift_instant
from tree_options.trex.snaptrade_runtime import SnapTradePaperRuntime
from tree_options.trex.supervised import SupervisedPaths, SupervisedRefused, _atomic_write, _locked


def initialize_binding(
    path: Path, *, auth_mode: Literal["commercial", "personal"] = "commercial"
) -> None:
    """Create a nonfunctional private template, exclusively and without SDK calls."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {
        "binding": {
            "alias": "alpaca-paper-canary",
            "account_id": "",
            "brokerage_slug": "ALPACA-PAPER",
            "paper_confirmed_by": "",
            "environment": "broker_paper",
            "live_money": False,
        },
        "credentials": dict.fromkeys(("client_id", "consumer_key", "user_id", "user_secret"), ""),
    }
    if auth_mode == "personal":
        payload["credentials"] = {"auth_mode": "personal", "client_id": "", "consumer_key": ""}
    elif auth_mode != "commercial":
        raise ValueError("unsupported authentication mode")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(payload, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _base_receipt(now: datetime) -> dict[str, Any]:
    return {
        "schema": "trex.snaptrade.read-only-qualification/v1",
        "sdk_version": SDK_VERSION,
        "assessed_at": now.isoformat(),
        "expires_at": shift_instant(now, 30).isoformat(),
        "verdict": "BLOCKED",
        "environment": "BROKER PAPER",
        "orders_authorized": False,
        "exact_economics": False,
        "live_money": False,
        "owner_held_at_assessment": False,
        "owner_released_after_assessment": False,
        "observations": [],
        "findings": [],
        "order_query": {"state": "all", "days": 7},
    }


def _persist(paths: SupervisedPaths, receipt: dict[str, Any]) -> None:
    # Unique historical receipts survive a later failed attempt or restart.
    content = canonical_bytes(receipt)
    content_id = hashlib.sha256(content).hexdigest()
    with _locked(paths):
        historical = paths.root / "qualification" / (content_id + ".json")
        if historical.exists():
            if historical.read_bytes() != content:
                raise SupervisedRefused("qualification_receipt_identity_collision")
        else:
            _atomic_write(historical, receipt)
        _atomic_write(paths.root / "read-only-qualification.json", receipt)


def qualify_read_only(runtime: SnapTradePaperRuntime) -> dict[str, Any]:
    receipt = _base_receipt(runtime.clock())
    receipt.update(
        account_alias=runtime.provider.binding.alias,
        provider_account_sha256=hashlib.sha256(
            runtime.provider.binding.account_id.encode()
        ).hexdigest(),
        owner_epoch=runtime.owner.epoch,
        state_root=str(runtime.paths.root.resolve()),
        ownership_root=str(runtime.owner.root.resolve()),
    )
    if runtime.owner.held or runtime.provider_owner.held or runtime.state_owner.held:
        receipt["findings"] = ["qualification_runtime_already_started"]
        _persist(runtime.paths, receipt)
        return receipt
    try:
        runtime.start()
        now = runtime.clock()
        receipt.update(assessed_at=now.isoformat(), expires_at=shift_instant(now, 30).isoformat())
        receipt["owner_held_at_assessment"] = (
            runtime.owner.held and runtime.provider_owner.held and runtime.state_owner.held
        )
        account = runtime.account
        if account is None:
            raise ProviderUnavailable("no account observations")
        findings = list(account_findings(account, now))
        if not runtime.ready:
            findings.append("runtime_reconciliation_not_ready")
        receipt["holdings_at"] = account.holdings_at.isoformat()
        oldest = min(
            account.holdings_at,
            account.details.captured_at,
            account.balances.captured_at,
            account.positions.captured_at,
            account.orders.captured_at,
            account.connection.captured_at if account.connection is not None else now,
        )
        receipt["expires_at"] = shift_instant(oldest, 30).isoformat()
        for name in ("details", "connection", "balances", "positions", "orders"):
            observation = getattr(account, name)
            if observation is None:
                findings.append("connection_observation_missing")
                continue
            receipt["observations"].append(
                {
                    "operation": name,
                    "captured_at": observation.captured_at.isoformat(),
                    "request_id": observation.request_id,
                    "digest": observation.digest,
                    "row_count": len(observation.body)
                    if isinstance(observation.body, list)
                    else None,
                }
            )
            if not observation.request_id:
                findings.append("provider_request_id_unavailable")
        receipt["execution_reconciliation"] = [
            {
                key: row[key]
                for key in (
                    "intent_id",
                    "state",
                    "reconciliation_clean",
                    "findings",
                    "evidence_verdict",
                    "exact_economics",
                )
            }
            for row in runtime.projection()["executions"]
        ]
        receipt["findings"] = list(dict.fromkeys(findings))
        if not findings and receipt["owner_held_at_assessment"]:
            receipt["verdict"] = "QUALIFIED"
    except SupervisedRefused as error:
        receipt["findings"] = [error.reason]
    except Exception:
        # Provider exceptions may contain signed URLs or credentials. Never
        # include their text, traceback, raw metadata or response body.
        receipt["findings"] = ["account_observation_or_recovery_unavailable"]
    finally:
        try:
            runtime.close()
        except Exception:
            receipt["verdict"] = "BLOCKED"
            receipt["findings"].append("local_projection_unavailable")
        receipt["owner_released_after_assessment"] = (
            not runtime.owner.held
            and not runtime.provider_owner.held
            and not runtime.state_owner.held
        )
    _persist(runtime.paths, receipt)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline SnapTrade binding setup and read-only paper qualification"
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path)
    parser.add_argument(
        "--auth-mode",
        choices=("commercial", "personal"),
        default="commercial",
        help="Template authentication mode; default preserves existing commercial setup",
    )
    parser.add_argument("--ownership-root", type=Path)
    parser.add_argument("command", choices=("init-binding", "check-config", "qualify"))
    args = parser.parse_args(argv)
    if args.command == "init-binding":
        try:
            initialize_binding(args.config, auth_mode=args.auth_mode)
        except OSError:
            print("Private template was not created; existing files are never overwritten.")
            return 2
        print("Private blank template created. Configure it locally; no provider was contacted.")
        return 0
    if args.command == "check-config":
        try:
            load_private_binding(args.config)
        except ProviderUnavailable:
            print("Private binding is missing or invalid; no provider was contacted.")
            return 2
        print(
            "Private binding structure and permissions validated offline; account identity remains unverified."
        )
        return 0
    if args.state is None:
        parser.error("qualify requires --state")
    paths = SupervisedPaths(args.state)
    try:
        provider = SnapTradeProvider.from_private_file(args.config)
    except ProviderUnavailable:
        receipt = _base_receipt(datetime.now(UTC))
        receipt["findings"] = ["private_binding_unavailable"]
        _persist(paths, receipt)
    else:
        receipt = qualify_read_only(
            SnapTradePaperRuntime(provider, paths, ownership_root=args.ownership_root)
        )
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["verdict"] == "QUALIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
