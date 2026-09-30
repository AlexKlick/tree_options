"""Durable paper proposals and explicit local operational-canary control.

HTTP creates proposals, never a broker session or execution authority. The local
owner reuses SnapTradePaperRuntime's account fence, recovery, mandate and permits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import Field, StrictStr, model_validator

from tree_options.action_graph.proposal import canonical_bytes
from tree_options.execution.records import OrderIntent
from tree_options.execution.snaptrade_provider import (
    EquityPaperEffect,
    SnapTradeProvider,
    load_private_binding,
)
from tree_options.schemas.common import StrictModel
from tree_options.time.sessions import shift_instant
from tree_options.trex.snaptrade_qualification import qualify_read_only
from tree_options.trex.snaptrade_runtime import SnapTradePaperRuntime
from tree_options.trex.supervised import (
    SupervisedPaths,
    SupervisedRefused,
    _atomic_write,
    _locked,
    grant_mandate,
    halt_effects,
    revoke_mandate,
)


class WorkspaceRefused(ValueError):
    """Safe local domain refusal code, with no provider exception content."""


class DeploymentRequest(StrictModel):
    idempotency_key: StrictStr = Field(min_length=1, max_length=128)
    account_alias: StrictStr = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")
    strategy_version: StrictStr = Field(min_length=1, max_length=128)
    research_job_id: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    sleeve_id: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    intended_capital_usd: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    max_gross_notional_usd: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    max_orders: int = Field(strict=True, ge=1, le=1000)
    ttl_seconds: int = Field(strict=True, ge=60, le=900)

    @model_validator(mode="after")
    def capital_bounds(self) -> DeploymentRequest:
        if self.max_gross_notional_usd > self.intended_capital_usd:
            raise ValueError("gross notional exceeds intended capital")
        return self


def _instant(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("naive timestamp")
    return result


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _codes(value: Any) -> list[str]:
    if not isinstance(value, list):
        return ["account_receipt_invalid"]
    return [
        item
        if isinstance(item, str) and re.fullmatch(r"[a-z_]+", item)
        else "account_receipt_invalid"
        for item in value
    ]


class PaperWorkspace:
    def __init__(
        self,
        root: Path,
        *,
        catalog: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.root = root
        self.paths = SupervisedPaths(root)
        self.catalog = catalog
        self.clock = clock or (lambda: datetime.now(UTC))

    def catalog_entries(self) -> list[dict[str, str]]:
        if self.catalog is None:
            return []
        try:
            fd = os.open(self.catalog, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd) as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                ):
                    raise ValueError
                source = json.load(stream)
            if (
                set(source) != {"accounts"}
                or not isinstance(source["accounts"], list)
                or len(source["accounts"]) > 32
            ):
                raise ValueError
            aliases: set[str] = set()
            roots: set[str] = set()
            for entry in source["accounts"]:
                provider = entry.get("provider", "snaptrade")
                path_keys: tuple[str, ...]
                if provider == "ibkr":
                    if set(entry) != {"provider", "account_alias", "account_id", "state_root"}:
                        raise ValueError
                    path_keys = ("state_root",)
                elif provider == "snaptrade":
                    if set(entry) not in (
                        {"account_alias", "binding_file", "state_root"},
                        {"provider", "account_alias", "binding_file", "state_root"},
                    ):
                        raise ValueError
                    path_keys = ("binding_file", "state_root")
                else:
                    raise ValueError
                if not all(isinstance(value, str) and value for value in entry.values()):
                    raise ValueError
                alias = entry["account_alias"]
                if provider == "ibkr" and alias.casefold() == entry["account_id"].casefold():
                    raise ValueError
                if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", alias):
                    raise ValueError
                state_root = str(Path(entry["state_root"]).resolve())
                if (
                    alias in aliases
                    or state_root in roots
                    or not all(Path(entry[k]).is_absolute() for k in path_keys)
                ):
                    raise ValueError
                aliases.add(alias)
                roots.add(state_root)
            return source["accounts"]
        except Exception:
            raise WorkspaceRefused("private_catalog_unavailable") from None

    def _entry(self, alias: str) -> dict[str, str]:
        entries = [entry for entry in self.catalog_entries() if entry["account_alias"] == alias]
        if len(entries) != 1:
            raise WorkspaceRefused("account_not_configured")
        return entries[0]

    def accounts(self) -> dict[str, Any]:
        result: dict[str, Any] = dict(
            schema="trex.paper.accounts/v1",
            environment="BROKER PAPER",
            live_money=False,
            setup_required=True,
            accounts=[],
            blockers=[],
        )
        try:
            entries = self.catalog_entries()
        except WorkspaceRefused as error:
            result["blockers"] = [str(error)]
            return result
        for entry in entries:
            row: dict[str, Any] = dict(
                account_alias=entry["account_alias"],
                provider=entry.get("provider", "snaptrade"),
                tradeable=False,
                equity_execution_ready=False,
                execution_scope="quant_equities_workspace",
                configured=False,
                qualification_status="BLOCKED",
                assessed_at=None,
                expires_at=None,
                owner_held=False,
                blockers=[],
            )
            if row["provider"] == "ibkr":
                result["accounts"].append(self._ibkr_account_view(entry, row))
                continue
            try:
                binding, _ = load_private_binding(Path(entry["binding_file"]))
                if binding.alias != row["account_alias"]:
                    raise ValueError
                row["configured"] = True
            except Exception:
                row["blockers"] = ["private_binding_unavailable"]
                result["accounts"].append(row)
                continue
            try:
                receipt = json.loads(
                    (Path(entry["state_root"]) / "read-only-qualification.json").read_text()
                )
                if (
                    receipt["schema"] != "trex.snaptrade.read-only-qualification/v1"
                    or receipt["account_alias"] != binding.alias
                    or receipt["provider_account_sha256"]
                    != hashlib.sha256(binding.account_id.encode()).hexdigest()
                    or receipt["state_root"] != str(Path(entry["state_root"]).resolve())
                    or receipt["environment"] != "BROKER PAPER"
                    or receipt["live_money"] is not False
                    or receipt["orders_authorized"] is not False
                    or receipt["exact_economics"] is not False
                ):
                    raise ValueError
                assessed, expires = (
                    _instant(receipt["assessed_at"]),
                    _instant(receipt["expires_at"]),
                )
                if (
                    not assessed <= self.clock() < expires
                    or (expires - assessed).total_seconds() > 30
                ):
                    row["blockers"] = ["account_qualification_stale"]
                else:
                    row["assessed_at"], row["expires_at"] = (
                        assessed.isoformat(),
                        expires.isoformat(),
                    )
                    row["blockers"] = _codes(receipt["findings"])
                    if (
                        receipt["verdict"] == "QUALIFIED"
                        and not row["blockers"]
                        and receipt["owner_held_at_assessment"] is True
                    ):
                        row["qualification_status"] = "QUALIFIED_AT_ASSESSMENT"
                    else:
                        row["blockers"].append("account_not_qualified")
                # A completed one-shot qualification releases its leases; this
                # field must not claim continuous ownership from its receipt.
            except Exception:
                row["blockers"] = ["account_qualification_required"]
            result["accounts"].append(row)
        result["setup_required"] = not entries or any(
            not account["configured"] for account in result["accounts"]
        )
        if not entries:
            result["blockers"] = ["account_setup_required"]
        return result

    def _ibkr_account_view(self, entry: dict[str, str], row: dict[str, Any]) -> dict[str, Any]:
        """Project an existing owner's read-only receipt; never connect or own IBKR."""
        row["configured"] = True
        row["blockers"] = ["ibkr_equity_execution_unimplemented"]
        try:
            receipt = json.loads(
                (Path(entry["state_root"]) / "read-only-qualification.json").read_text()
            )
            if (
                receipt["schema"] != "trex.ibkr.read-only-qualification/v1"
                or receipt["provider"] != "ibkr"
                or receipt["account_alias"] != entry["account_alias"]
                or receipt["provider_account_sha256"]
                != hashlib.sha256(entry["account_id"].encode()).hexdigest()
                or receipt["state_root"] != str(Path(entry["state_root"]).resolve())
                or receipt["environment"] != "BROKER PAPER"
                or any(
                    receipt[key] is not False
                    for key in (
                        "orders_authorized",
                        "live_money",
                        "exact_economics",
                        "equity_execution_ready",
                    )
                )
                or receipt["paper_verified"] is not True
                or receipt["ownership_verified_at_assessment"] is not True
                or not isinstance(receipt["owner_epoch"], str)
                or not receipt["owner_epoch"]
            ):
                raise ValueError
            assessed, expires = _instant(receipt["assessed_at"]), _instant(receipt["expires_at"])
            if not assessed <= self.clock() < expires or (expires - assessed).total_seconds() > 30:
                row["blockers"].append("account_qualification_stale")
                return row
            observations = receipt["observations"]
            if (
                not isinstance(observations, list)
                or len(observations) != 4
                or any(not isinstance(observation, dict) for observation in observations)
            ):
                raise ValueError
            if sorted(observation["operation"] for observation in observations) != [
                "balances",
                "completed_orders",
                "orders",
                "positions",
            ]:
                raise ValueError
            captured_times = []
            for observation in observations:
                captured = _instant(observation["captured_at"])
                if captured > assessed or not 0 <= (self.clock() - captured).total_seconds() <= 30:
                    raise ValueError
                if (
                    not isinstance(observation["digest"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", observation["digest"])
                    or type(observation["row_count"]) is not int
                    or observation["row_count"] < 0
                    or (observation["operation"] == "balances" and observation["row_count"] != 3)
                    or observation["broker_event_at"] is not None
                ):
                    raise ValueError
                captured_times.append(captured)
            if expires > shift_instant(min(captured_times), 30):
                raise ValueError
            row["assessed_at"], row["expires_at"] = assessed.isoformat(), expires.isoformat()
            findings = _codes(receipt["findings"])
            row["blockers"].extend(findings)
            if receipt["verdict"] == "QUALIFIED" and not findings:
                row["qualification_status"] = "QUALIFIED_AT_ASSESSMENT"
            else:
                row["blockers"].append("account_not_qualified")
        except Exception:
            row["blockers"].append("ibkr_read_only_qualification_required")
        return row

    def create_allocation(
        self, idempotency_key: str, *, account_alias: str | None
    ) -> dict[str, Any]:
        if not idempotency_key or len(idempotency_key) > 128:
            raise WorkspaceRefused("allocation_request_invalid")
        if account_alias is not None and not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", account_alias
        ):
            raise WorkspaceRefused("allocation_request_invalid")
        request = dict(idempotency_key=idempotency_key, account_alias=account_alias)
        with _locked(self.paths):
            path = self.root / "allocation-plan.json"
            if path.exists():
                plan = json.loads(path.read_text())
                if plan["request"] != request:
                    raise WorkspaceRefused("allocation_plan_already_exists")
            else:
                plan_id = _hash(request)
                plan = dict(
                    plan_id=plan_id,
                    request=request,
                    sleeves=[
                        dict(
                            sleeve_id=f"sleeve-{index + 1:02}",
                            label=f"Experiment {index + 1:02}",
                            capital_usd="50000" if index < 19 else "5000",
                            account_alias=account_alias,
                        )
                        for index in range(29)
                    ],
                )
                _atomic_write(path, plan)
            return self._allocation_view(plan)

    def _allocation_view(self, plan: dict[str, Any]) -> dict[str, Any]:
        if plan["plan_id"] != _hash(plan["request"]) or len(plan["sleeves"]) != 29:
            raise WorkspaceRefused("allocation_conservation_invalid")
        for index, sleeve in enumerate(plan["sleeves"]):
            if sleeve != dict(
                sleeve_id=f"sleeve-{index + 1:02}",
                label=f"Experiment {index + 1:02}",
                capital_usd="50000" if index < 19 else "5000",
                account_alias=plan["request"]["account_alias"],
            ):
                raise WorkspaceRefused("allocation_conservation_invalid")
        records = [
            self._read(path.stem) for path in sorted((self.root / "deployments").glob("*.json"))
        ]
        sleeves = []
        for sleeve in plan["sleeves"]:
            applicable = [
                record
                for record in records
                if record["request"].get("sleeve_id") == sleeve["sleeve_id"]
                and not record.get("reservation_released", False)
            ]
            aliases = {record["request"]["account_alias"] for record in applicable}
            account_alias = plan.get("bindings", {}).get(
                sleeve["sleeve_id"], sleeve["account_alias"]
            )
            if account_alias is not None:
                aliases.add(account_alias)
            if len(aliases) > 1:
                raise WorkspaceRefused("sleeve_account_mismatch")
            reserved = sum(
                (
                    Decimal(record["request"]["max_gross_notional_usd"])
                    for record in applicable
                    if not record.get("reservation_released", False)
                ),
                Decimal("0"),
            )
            if reserved > Decimal(sleeve["capital_usd"]):
                raise WorkspaceRefused("sleeve_capital_exhausted")
            sleeves.append(
                sleeve | dict(account_alias=next(iter(aliases), None), reserved_usd=str(reserved))
            )
        if sum((Decimal(sleeve["capital_usd"]) for sleeve in sleeves), Decimal("0")) != Decimal(
            "1000000"
        ):
            raise WorkspaceRefused("allocation_conservation_invalid")
        return dict(
            plan_id=plan["plan_id"],
            total_capital_usd="1000000",
            sleeves=sleeves,
            execution_authorized=False,
            live_money=False,
            scope="virtual_funding_reservations",
            broker_balance_verified=False,
            exact_economics=False,
        )

    def allocations(self) -> dict[str, Any]:
        path = self.root / "allocation-plan.json"
        return dict(
            schema="trex.paper.allocations/v1",
            plans=[self._allocation_view(json.loads(path.read_text()))] if path.exists() else [],
            live_money=False,
        )

    def bind_sleeve(self, plan_id: str, sleeve_id: str, account_alias: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", account_alias):
            raise WorkspaceRefused("account_alias_invalid")
        with _locked(self.paths):
            path = self.root / "allocation-plan.json"
            if not path.exists():
                raise WorkspaceRefused("allocation_plan_not_found")
            plan = json.loads(path.read_text())
            view = self._allocation_view(plan)
            if view["plan_id"] != plan_id:
                raise WorkspaceRefused("allocation_plan_not_found")
            selected = [sleeve for sleeve in view["sleeves"] if sleeve["sleeve_id"] == sleeve_id]
            if len(selected) != 1:
                raise WorkspaceRefused("sleeve_not_found")
            if selected[0]["account_alias"] == account_alias:
                return view
            if Decimal(selected[0]["reserved_usd"]) != 0:
                raise WorkspaceRefused("sleeve_has_active_or_uncertain_reservations")
            plan.setdefault("bindings", {})[sleeve_id] = account_alias
            _atomic_write(path, plan)
            return self._allocation_view(plan)

    def _check_sleeve(self, request: DeploymentRequest) -> None:
        if request.strategy_version != "operational-canary/1" and request.research_job_id is None:
            raise WorkspaceRefused("research_job_required")
        if request.sleeve_id is None:
            if request.strategy_version != "operational-canary/1":
                raise WorkspaceRefused("sleeve_assignment_required")
            return
        plans = self.allocations()["plans"]
        sleeves = [
            sleeve
            for plan in plans
            for sleeve in plan["sleeves"]
            if sleeve["sleeve_id"] == request.sleeve_id
        ]
        if len(sleeves) != 1:
            raise WorkspaceRefused("sleeve_not_found")
        sleeve = sleeves[0]
        if sleeve["account_alias"] not in {None, request.account_alias}:
            raise WorkspaceRefused("sleeve_account_mismatch")
        if request.intended_capital_usd > Decimal(sleeve["capital_usd"]):
            raise WorkspaceRefused("intended_capital_exceeds_sleeve")
        if request.max_gross_notional_usd + Decimal(sleeve["reserved_usd"]) > Decimal(
            sleeve["capital_usd"]
        ):
            raise WorkspaceRefused("sleeve_capital_exhausted")

    def _path(self, deployment_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", deployment_id):
            raise WorkspaceRefused("deployment_not_found")
        return self.root / "deployments" / f"{deployment_id}.json"

    def _read(self, deployment_id: str) -> dict[str, Any]:
        try:
            record = json.loads(self._path(deployment_id).read_text())
            request = DeploymentRequest.model_validate(record["request"])
            if _hash(request.idempotency_key) != deployment_id or record["request_sha256"] != _hash(
                record["request"]
            ):
                raise ValueError
            return record
        except (OSError, KeyError, ValueError):
            raise WorkspaceRefused("deployment_not_found_or_corrupt") from None

    def _view(self, record: dict[str, Any]) -> dict[str, Any]:
        if (self.root / "halts" / f"{record['deployment_id']}.json").exists():
            record = record | dict(
                status="HALTED", execution_status="HALTED_RECONCILIATION_REQUIRED"
            )
        request = record["request"]
        blockers = ["operator_review_required"]
        if request["strategy_version"] != "operational-canary/1":
            blockers += [
                "strategy_deployment_unqualified",
                "historical_data_unqualified",
                "persistent_portfolio_execution_unimplemented",
            ]
        if request["strategy_version"] != "operational-canary/1" and not request.get("sleeve_id"):
            blockers.append("sleeve_assignment_required")
        if request["strategy_version"] != "operational-canary/1" and not request.get(
            "research_job_id"
        ):
            blockers.append("research_job_required")
        account = next(
            (
                row
                for row in self.accounts()["accounts"]
                if row["account_alias"] == request["account_alias"]
            ),
            None,
        )
        blockers.extend(account["blockers"] if account else ["account_not_configured"])
        if record["status"] == "HALTED":
            blockers.append("deployment_halted")
        return {key: value for key, value in request.items() if key != "idempotency_key"} | dict(
            deployment_id=record["deployment_id"],
            status=record["status"],
            created_at=record["created_at"],
            execution_status=record.get("execution_status", "NOT_AUTHORIZED"),
            blockers=list(dict.fromkeys(blockers)),
            environment="BROKER PAPER",
            live_money=False,
            exact_economics=False,
        )

    def propose(self, request: DeploymentRequest) -> dict[str, Any]:
        deployment_id = _hash(request.idempotency_key)
        with _locked(self.paths):
            if self._path(deployment_id).exists():
                record = self._read(deployment_id)
                if record["request_sha256"] != _hash(request.model_dump(mode="json")):
                    raise WorkspaceRefused("idempotency_collision")
            else:
                self._check_sleeve(request)
                payload = request.model_dump(mode="json")
                record = dict(
                    deployment_id=deployment_id,
                    request=payload,
                    request_sha256=_hash(payload),
                    status="REVIEW_REQUIRED",
                    created_at=self.clock().isoformat(),
                )
                _atomic_write(self._path(deployment_id), record)
            return self._view(record)

    def deployments(self) -> dict[str, Any]:
        return dict(
            schema="trex.paper.deployments/v1",
            live_money=False,
            deployments=[
                self._view(self._read(path.stem))
                for path in sorted((self.root / "deployments").glob("*.json"))
            ],
        )

    def halt(self, deployment_id: str) -> dict[str, Any]:
        record = self._read(deployment_id)
        # The kill files are durable before any locks. Halt stays available when
        # an effect owner is waiting on an account read or broker response.
        _atomic_write(
            self.root / "halts" / f"{deployment_id}.json", {"at": self.clock().isoformat()}
        )
        try:
            entry = self._entry(record["request"]["account_alias"])
        except WorkspaceRefused:
            entry = None
        revocation_pending = False
        if entry and entry.get("provider", "snaptrade") == "snaptrade":
            paths = SupervisedPaths(Path(entry["state_root"]))
            halt_effects(paths, now=self.clock(), reason="operator_workspace_halt")
            try:
                revoke_mandate(
                    paths, now=self.clock(), reason="operator_workspace_halt", blocking=False
                )
            except SupervisedRefused as error:
                if error.reason == "state_busy":
                    revocation_pending = True
                elif error.reason != "mandate_absent":
                    raise
        try:
            with _locked(self.paths, blocking=False):
                record = self._read(deployment_id)
                if (
                    record["status"] in {"REVIEW_REQUIRED", "STAGED"}
                    and record.get("execution_status", "NOT_AUTHORIZED") == "NOT_AUTHORIZED"
                ):
                    record["reservation_released"] = True
                record["status"] = "HALTED"
                record["execution_status"] = "HALTED_RECONCILIATION_REQUIRED"
                _atomic_write(self._path(deployment_id), record)
        except SupervisedRefused as error:
            if error.reason != "state_busy":
                raise
        return self._view(record) | dict(mandate_revocation_pending=revocation_pending)

    def stage_canary(
        self, deployment_id: str, *, symbol: str, limit: Decimal, operator_approved: bool = False
    ) -> dict[str, Any]:
        if not operator_approved:
            raise WorkspaceRefused("operator_approval_required")
        with _locked(self.paths):
            record = self._read(deployment_id)
            request = DeploymentRequest.model_validate(record["request"])
            try:
                entry = self._entry(request.account_alias)
            except WorkspaceRefused as error:
                if str(error) != "account_not_configured":
                    raise
                entry = None
            if entry and entry.get("provider", "snaptrade") != "snaptrade":
                raise WorkspaceRefused("ibkr_equity_execution_unimplemented")
            if (
                record["status"] == "HALTED"
                or (self.root / "halts" / f"{deployment_id}.json").exists()
            ):
                raise WorkspaceRefused("deployment_halted")
            if request.strategy_version != "operational-canary/1":
                raise WorkspaceRefused("strategy_deployment_unqualified")
            if (
                request.max_orders != 1
                or request.max_gross_notional_usd > Decimal("100")
                or not limit.is_finite()
                or not Decimal("0") < limit <= request.max_gross_notional_usd
            ):
                raise WorkspaceRefused("canary_bounds")
            # Validate exact supported semantics with the existing domain.
            effect = EquityPaperEffect(
                intent_id="paper-" + deployment_id[:32],
                account_alias=request.account_alias,
                owner_epoch="staged",
                strategy_version=request.strategy_version,
                symbol=symbol,
                quantity=1,
                limit=limit,
            )
            approval = dict(
                request_sha256=record["request_sha256"],
                symbol=effect.symbol,
                quantity=1,
                side="BUY",
                limit=str(effect.limit),
            )
            if "approval" in record:
                if record["status"] != "STAGED":
                    raise WorkspaceRefused("deployment_not_reviewable_reconcile_before_retry")
                if {key: record["approval"][key] for key in approval} != approval:
                    raise WorkspaceRefused("approval_identity_collision")
            else:
                if record["status"] != "REVIEW_REQUIRED":
                    raise WorkspaceRefused("deployment_not_reviewable")
                approval.update(
                    approved_at=self.clock().isoformat(),
                    expires_at=shift_instant(self.clock(), request.ttl_seconds).isoformat(),
                    approved_by="operator_local_cli",
                )
                record["approval"] = approval
                record["status"] = "STAGED"
                _atomic_write(self._path(deployment_id), record)
            return self._view(record)

    def execute_canary(self, deployment_id: str, runtime: SnapTradePaperRuntime) -> dict[str, Any]:
        """Claim once, recover/read broker state, then use existing effect owner.

        An interrupted claim is recovery-only, even if it failed before the provider
        call. Repeating this command never attempts a second economic order.
        """
        with _locked(self.paths):
            record = self._read(deployment_id)
            if (
                record["status"] != "STAGED"
                or (self.root / "halts" / f"{deployment_id}.json").exists()
            ):
                raise WorkspaceRefused("deployment_not_staged_reconcile_before_retry")
            request = DeploymentRequest.model_validate(record["request"])
            entry = self._entry(request.account_alias)
            if entry.get("provider", "snaptrade") != "snaptrade":
                raise WorkspaceRefused("ibkr_equity_execution_unimplemented")
            approval = record["approval"]
            if (
                approval["request_sha256"] != record["request_sha256"]
                or runtime.provider.binding.alias != request.account_alias
            ):
                raise WorkspaceRefused("approval_account_binding_mismatch")
            if runtime.paths.root.resolve() != Path(entry["state_root"]).resolve():
                raise WorkspaceRefused("runtime_state_binding_mismatch")
            binding, _ = load_private_binding(Path(entry["binding_file"]))
            if runtime.provider.binding != binding:
                raise WorkspaceRefused("runtime_provider_binding_mismatch")
            if (
                not _instant(approval["approved_at"])
                <= self.clock()
                < _instant(approval["expires_at"])
            ):
                raise WorkspaceRefused("approval_expired")
            if runtime.quote_source is None:
                raise WorkspaceRefused("timestamped_quote_source_unavailable")
            record["status"] = "RECOVERY_REQUIRED"
            record["execution_status"] = "CLAIMED_RECONCILIATION_REQUIRED"
            _atomic_write(self._path(deployment_id), record)
            try:
                runtime.start()  # recovery and external readback precede effects
                if not runtime.ready:
                    raise WorkspaceRefused("restart_reconciliation_required")
                runtime._graph(
                    "deployment",
                    "paper-" + deployment_id[:32],
                    {
                        "deployment_id": deployment_id,
                        "request_sha256": record["request_sha256"],
                        "approval_sha256": _hash(approval),
                        "research_job_id": request.research_job_id,
                        "sleeve_id": request.sleeve_id,
                        "account_alias": request.account_alias,
                    },
                )
                remaining = int((_instant(approval["expires_at"]) - self.clock()).total_seconds())
                if remaining < 60:
                    raise WorkspaceRefused("approval_ttl_insufficient")
                grant_mandate(
                    runtime.paths,
                    now=self.clock(),
                    account_id=request.account_alias,
                    owner_epoch=runtime.owner.epoch,
                    strategy_version=request.strategy_version,
                    profile_digest=_hash(approval),
                    max_orders=request.max_orders,
                    ttl_seconds=min(remaining, request.ttl_seconds),
                    granted_by="operator_local_cli",
                    environment="broker_paper",
                    max_gross_notional_usd=request.max_gross_notional_usd,
                )
                effect = EquityPaperEffect(
                    intent_id="paper-" + deployment_id[:32],
                    account_alias=request.account_alias,
                    owner_epoch=runtime.owner.epoch,
                    strategy_version=request.strategy_version,
                    symbol=approval["symbol"],
                    quantity=approval["quantity"],
                    limit=Decimal(approval["limit"]),
                )
                intent = OrderIntent(
                    intent_id=effect.intent_id,
                    contract_id=effect.symbol,
                    side="BUY",
                    position_effect="OPEN_LONG",
                    quantity=1,
                    order_type="LIMIT",
                    limit_price=effect.limit,
                    intent_created_at=self.clock(),
                    source=effect.strategy_version,
                    source_sequence_id=deployment_id,
                )
                outcome = runtime.canary(intent, effect, operator_approved=True)
                record["execution_status"] = outcome["outcome"].upper() + "_RECONCILIATION_REQUIRED"
                record["status"] = (
                    "OBSERVED" if outcome["outcome"] == "acknowledged" else "RECOVERY_REQUIRED"
                )
                _atomic_write(self._path(deployment_id), record)
                return self._view(record)
            finally:
                runtime.close()


def workspace_from_environment() -> PaperWorkspace:
    root = Path(
        os.environ.get(
            "TREX_PAPER_WORKSPACE", str(Path.home() / ".local/state/trex/paper-workspace")
        )
    )
    catalog = os.environ.get("TREX_PAPER_CATALOG")
    return PaperWorkspace(root, catalog=Path(catalog) if catalog else None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Local paper owner controls; no browser-owned broker"
    )
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--ownership-root", type=Path, required=True)
    parser.add_argument(
        "command",
        choices=("status", "qualify", "stage-canary", "execute-canary", "recover", "halt"),
    )
    parser.add_argument("--account-alias")
    parser.add_argument("--deployment-id")
    parser.add_argument("--symbol")
    parser.add_argument("--limit", type=Decimal)
    parser.add_argument("--operator-approved", action="store_true")
    parser.add_argument(
        "--quote-provider",
        choices=("massive",),
        help="Explicit authoritative quote source for canary execution only",
    )
    args = parser.parse_args(argv)
    workspace = PaperWorkspace(args.workspace, catalog=args.catalog)
    result: dict[str, Any]
    try:
        if args.command == "status":
            result = dict(accounts=workspace.accounts(), deployments=workspace.deployments())
        elif args.command == "halt":
            result = workspace.halt(args.deployment_id or "")
        elif args.command == "stage-canary":
            if args.symbol is None or args.limit is None:
                raise WorkspaceRefused("exact_order_required")
            result = workspace.stage_canary(
                args.deployment_id or "",
                symbol=args.symbol,
                limit=args.limit,
                operator_approved=args.operator_approved,
            )
        else:
            entry = workspace._entry(args.account_alias or "")
            if entry.get("provider", "snaptrade") != "snaptrade":
                raise WorkspaceRefused("ibkr_qualification_requires_existing_owner")
            provider = SnapTradeProvider.from_private_file(Path(entry["binding_file"]))
            runtime = SnapTradePaperRuntime(
                provider,
                SupervisedPaths(Path(entry["state_root"])),
                ownership_root=args.ownership_root,
            )
            if args.command == "execute-canary" and args.quote_provider == "massive":
                from tree_options.trex.massive_paper_quotes import quote_source_from_environment

                runtime.quote_source = quote_source_from_environment(clock=runtime.clock)
            if args.command == "qualify":
                result = qualify_read_only(runtime)
            elif args.command == "execute-canary":
                # A CLI input file/local receipt time cannot substitute for an
                # authoritative quote feed. Default execution remains refused.
                result = workspace.execute_canary(args.deployment_id or "", runtime)
            else:
                try:
                    runtime.start()
                    runtime.publish()
                    result = dict(
                        status="RECOVERED_READ_ONLY", ready=runtime.ready, live_money=False
                    )
                finally:
                    runtime.close()
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        reason = (
            str(error)
            if isinstance(error, WorkspaceRefused)
            else error.reason
            if isinstance(error, SupervisedRefused)
            else "paper_operation_refused"
        )
        reason = _codes([reason])[0]
        print(json.dumps(dict(status="BLOCKED", reason=reason, live_money=False)))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
