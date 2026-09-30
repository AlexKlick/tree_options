"""Alpaca Paper provider behind the existing supervised permit/outbox boundary.

The runtime owns the account lease and durable journals, never the browser/LLM.
Exact fill economics remain unavailable; there are no fabricated fill events.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tree_options.execution.evidence import assess_evidence
from tree_options.execution.lifecycle import ExecutionState
from tree_options.execution.reconciliation import reconcile
from tree_options.execution.records import BrokerReadback, OrderIntent, SubmitAttempt
from tree_options.execution.snaptrade_adapter import (
    acknowledgement_from_snapshot,
    readback_from_snapshot,
    rejection_from_snapshot,
    snapshot_from_mapping,
    stable_client_order_id,
)
from tree_options.execution.snaptrade_provider import (
    EquityPaperEffect,
    ProviderDisconnected,
    SnapTradeProvider,
)
from tree_options.research.runstate.store import RunstateStore
from tree_options.time.sessions import shift_instant
from tree_options.trex.account_ownership import AccountOwnership
from tree_options.trex.account_ownership import ownership_root as default_ownership_root
from tree_options.trex.supervised import (
    Acknowledged,
    EffectPermit,
    LookupUnknown,
    Refused,
    Submitted,
    SupervisedIntent,
    SupervisedPaths,
    SupervisedRefused,
    Uncertain,
    _append_line,
    _atomic_write,
    _journal_append,
    _locked,
    active_mandate,
    issue_permit,
    project_intent,
    reconcile_intent,
    record_intent,
    recover,
    revoke_mandate,
    send,
)
from tree_options.trex.supervised import (
    status as supervised_status,
)


@dataclass(frozen=True)
class CanaryQuote:
    symbol: str
    bid: Decimal
    ask: Decimal
    observed_at: datetime
    source_id: str

    def valid_at(self, now: datetime) -> bool:
        return (
            self.observed_at.tzinfo is not None
            and bool(self.source_id)
            and self.bid.is_finite()
            and self.ask.is_finite()
            and Decimal("0") < self.bid <= self.ask
            and 0 <= (now - self.observed_at).total_seconds() <= 15
        )


class SnapTradePaperRuntime:
    execution_environment = "broker_paper"

    def __init__(
        self,
        provider: SnapTradeProvider,
        paths: SupervisedPaths,
        *,
        ownership_root: Path | None = None,
        clock: Callable[[], datetime] | None = None,
        quote_source: Callable[[str], CanaryQuote] | None = None,
    ) -> None:
        self.provider = provider
        self.paths = paths
        self.clock = clock or (lambda: datetime.now(UTC))
        self.quote_source = quote_source
        self.owner = AccountOwnership(
            ownership_root or default_ownership_root(), provider.binding.alias
        )
        self.ready = False
        self._current: OrderIntent | None = None
        self._screening: dict[str, Any] = {}

    def start(self) -> None:
        self.owner.acquire()
        try:
            recover(self.paths, now=self.clock())
            self.refresh()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self.ready = False
        self.owner.close()
        self.publish()

    def refresh(self) -> None:
        if not self.owner.held:
            raise SupervisedRefused("account_owner_absent")
        account = self.provider.account_snapshot()
        _atomic_write(
            self.paths.root / "account-snapshot.json",
            {
                "account_alias": account.binding.alias,
                "environment": "broker_paper",
                "holdings_at": account.holdings_at.isoformat(),
                "observations": [
                    {
                        "body": json.loads(json.dumps(o.body, default=str)),
                        "captured_at": o.captured_at.isoformat(),
                        "request_id": o.request_id,
                        "digest": o.digest,
                    }
                    for o in (account.details, account.balances, account.positions, account.orders)
                ],
            },
        )
        self.ready = account.fresh_at(self.clock())
        for terminal in self.paths.outbox_dir().glob("*.terminal.json"):
            if not self.paths.journal(terminal.name.removesuffix(".terminal.json")).exists():
                self.ready = False  # orphan effect without a recoverable journal
        for journal in sorted((self.paths.root / "journal").glob("*.jsonl")):
            if journal.name.endswith(".reconcile.jsonl"):
                continue
            intent_id = journal.stem
            lifecycle = project_intent(self.paths, intent_id)
            lookup = self.lookup(intent_id)
            if isinstance(lookup, LookupUnknown):
                self.ready = False
            if isinstance(lookup, Submitted):
                with _locked(self.paths):
                    for fact in lookup.facts:
                        # Preserve one normalized fact for duplicate provider
                        # content, including its original receipt timestamp.
                        existing = next(
                            (
                                r
                                for r in lifecycle.records
                                if getattr(r, "record_id", None) == fact.record_id
                            ),
                            None,
                        )
                        if existing is not None and existing.model_dump(
                            exclude={"locally_received_at"}
                        ) != fact.model_dump(exclude={"locally_received_at"}):
                            lifecycle.apply(fact)  # raises canonical identity collision
                        if existing is None:
                            lifecycle = lifecycle.apply(fact)
                            _journal_append(self.paths, fact)
                            self._graph(
                                "observation",
                                intent_id,
                                {
                                    "record_id": fact.record_id,
                                    "broker_order_id": lookup.broker_order_id,
                                },
                            )
                if self.paths.terminal(intent_id).exists():
                    terminal = json.loads(self.paths.terminal(intent_id).read_text())
                    if terminal.get("outcome") == "uncertain":
                        try:
                            reconcile_intent(
                                self.paths, now=self.clock(), intent_id=intent_id, broker=self
                            )
                        except SupervisedRefused:
                            self.ready = False
            if not reconcile(lifecycle).is_clean or lifecycle.state not in {
                ExecutionState.CANCELED,
                ExecutionState.REJECTED,
            }:
                self.ready = False

    def validate_effect(
        self, payload: bytes, intent: OrderIntent, permit: EffectPermit | None = None
    ) -> None:
        effect = EquityPaperEffect.model_validate_json(payload)
        if effect.payload() != payload:
            raise SupervisedRefused("effect_bytes_not_canonical")
        now = self.clock()
        if not self.owner.held or effect.owner_epoch != self.owner.epoch:
            raise SupervisedRefused("account_owner_mismatch")
        if effect.account_alias != self.provider.binding.alias:
            raise SupervisedRefused("account_alias_mismatch")
        mandate = active_mandate(
            self.paths,
            now=now,
            account_id=effect.account_alias,
            owner_epoch=self.owner.epoch,
            strategy_version=effect.strategy_version,
        )
        if mandate.environment != self.execution_environment:
            raise SupervisedRefused("broker_environment_mismatch")
        if (
            mandate.max_gross_notional_usd is None
            or effect.limit * effect.quantity > mandate.max_gross_notional_usd
        ):
            raise SupervisedRefused("canary_notional_cap")
        if (
            effect.intent_id,
            effect.symbol,
            effect.quantity,
            effect.limit,
            effect.side,
            effect.strategy_version,
        ) != (
            intent.intent_id,
            intent.contract_id,
            intent.quantity,
            intent.limit_price,
            intent.side,
            intent.source,
        ):
            raise SupervisedRefused("effect_intent_binding_mismatch")
        if permit is not None and (
            permit.account_id != effect.account_alias or permit.owner_epoch != self.owner.epoch
        ):
            raise SupervisedRefused("permit_owner_mismatch")
        account = self.provider.account_snapshot()
        now = self.clock()
        if not account.fresh_at(now) or account.binding != self.provider.binding:
            raise SupervisedRefused("account_snapshot_stale")
        if not all(
            isinstance(observation.body, list)
            for observation in (account.balances, account.positions, account.orders)
        ):
            raise SupervisedRefused("account_snapshot_shape_invalid")
        for row in account.orders.body:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("status"), str)
                or not row["status"]
            ):
                raise SupervisedRefused("account_order_shape_invalid")
        # The initial canary is intentionally isolated: existing holdings or
        # working orders block rather than inventing an equity risk ledger.
        if account.positions.body or any(
            str(row.get("status", "")).upper()
            not in {"CANCELED", "REJECTED", "FAILED", "EXPIRED", "EXECUTED", "FILLED"}
            for row in account.orders.body
        ):
            raise SupervisedRefused("account_exposure_or_orders_present")
        usd_cash = Decimal("0")
        for row in account.balances.body:
            if not isinstance(row, dict) or not isinstance(row.get("currency"), dict):
                raise SupervisedRefused("account_balance_shape_invalid")
            code = row["currency"].get("code")
            if not isinstance(code, str) or not code:
                raise SupervisedRefused("account_balance_shape_invalid")
            try:
                cash = Decimal(str(row.get("cash")))
            except (InvalidOperation, ValueError, TypeError):
                raise SupervisedRefused("account_cash_invalid") from None
            if not cash.is_finite():
                raise SupervisedRefused("account_cash_invalid")
            if code == "USD":
                usd_cash += cash
        if usd_cash < effect.limit * effect.quantity:
            raise SupervisedRefused("insufficient_cash")
        if self.quote_source is None:
            # SDK quote schema has no authoritative timestamp. Never call a
            # local receive time a quote event time to unblock the canary.
            raise SupervisedRefused("timestamped_quote_source_unavailable")
        quote = self.quote_source(effect.symbol)
        now = self.clock()
        active_mandate(
            self.paths,
            now=now,
            account_id=effect.account_alias,
            owner_epoch=self.owner.epoch,
            strategy_version=effect.strategy_version,
        )
        if not account.fresh_at(now):
            raise SupervisedRefused("account_snapshot_stale")
        if permit is not None and now >= permit.expires_at:
            raise SupervisedRefused("permit_expired_after_preflight")
        if quote.symbol != effect.symbol or not quote.valid_at(now) or effect.limit > quote.ask:
            raise SupervisedRefused("quote_stale_or_invalid")
        self._screening = {
            "account_alias": effect.account_alias,
            "owner_epoch": self.owner.epoch,
            "mandate_id": mandate.mandate_id,
            "at": now.isoformat(),
            "quote_at": quote.observed_at.isoformat(),
            "quote_source_id": quote.source_id,
            "max_gross_notional_usd": str(mandate.max_gross_notional_usd),
            "reserved_notional_usd": str(effect.limit * effect.quantity),
            "cash_usd": str(usd_cash),
            "account_receipts": [
                {"digest": o.digest, "request_id": o.request_id}
                for o in (account.details, account.balances, account.positions, account.orders)
            ],
        }
        self._current = intent

    def canary(
        self,
        intent: OrderIntent,
        effect: EquityPaperEffect,
        *,
        operator_approved: bool = False,
        research_store: RunstateStore | None = None,
        campaign_id: str | None = None,
    ) -> dict[str, Any]:
        if not operator_approved:
            raise SupervisedRefused("canary_disabled_by_default")
        if not self.ready:
            raise SupervisedRefused("restart_reconciliation_required")
        payload = effect.payload()
        self.validate_effect(payload, intent)
        now = self.clock()
        package_hash = hashlib.sha256(payload).hexdigest()
        supervised = SupervisedIntent(
            intent=intent,
            package_intent_sha256=package_hash,
            created_at=now,
            send_deadline=shift_instant(now, 30),
        )
        record_intent(self.paths, supervised)
        research_refs = {}
        if campaign_id is not None:
            if research_store is None:
                raise SupervisedRefused("campaign_store_required")
            from tree_options.research.quant import research_execution_link

            research_refs = research_execution_link(research_store, campaign_id, intent.intent_id)
        self._graph(
            "proposal",
            intent.intent_id,
            {
                "strategy_version": intent.source,
                "effect_sha256": package_hash,
                "research": research_refs,
            },
        )
        self._graph("authorization", intent.intent_id, {"mandate": self._screening["mandate_id"]})
        screening_sha = hashlib.sha256(
            json.dumps(self._screening, sort_keys=True).encode()
        ).hexdigest()
        _atomic_write(self.paths.root / "risk" / f"{screening_sha}.json", self._screening)
        self._graph(
            "reservation",
            intent.intent_id,
            {"risk_snapshot": screening_sha, "notional": self._screening["reserved_notional_usd"]},
        )
        permit = issue_permit(
            self.paths,
            now=now,
            account_id=effect.account_alias,
            owner_epoch=self.owner.epoch,
            intent=supervised,
            canary_blockers=(),
            effect_payload=payload,
            screening_sha256=screening_sha,
            ttl_seconds=15,
        )
        self._graph("permit", intent.intent_id, {"permit_id": permit.permit_id})
        result = send(
            self.paths,
            now=self.clock(),
            permit_id=permit.permit_id,
            effect_payload=payload,
            broker=self,
        )
        self._graph(
            "execution",
            intent.intent_id,
            {"permit_id": permit.permit_id, "outcome": result["outcome"]},
        )
        lifecycle = project_intent(self.paths, intent.intent_id)
        self._graph("reconciliation", intent.intent_id, {"clean": reconcile(lifecycle).is_clean})
        self._graph(
            "evidence", intent.intent_id, {"verdict": assess_evidence(lifecycle).verdict.value}
        )
        self.ready = False
        self.publish()
        return result

    def bind_broker_order(self, broker_order_id: str, intent_id: str) -> None:
        if not self.owner.held:
            raise SupervisedRefused("account_owner_absent")
        path = self.paths.root / "broker-order-identities.json"
        identities = json.loads(path.read_text()) if path.exists() else {}
        if broker_order_id in identities and identities[broker_order_id] != intent_id:
            raise SupervisedRefused("broker_order_identity_collision")
        if any(
            observed_id != broker_order_id and bound_intent == intent_id
            for observed_id, bound_intent in identities.items()
        ):
            raise SupervisedRefused("broker_order_identity_changed")
        if broker_order_id in identities:
            return
        identities[broker_order_id] = intent_id
        _atomic_write(path, identities)

    def submit(
        self, attempt: SubmitAttempt, effect_payload: bytes
    ) -> Acknowledged | Refused | Uncertain:
        if self._current is None or self._current.intent_id != attempt.intent_id:
            return Uncertain("intent_not_preflighted", "no provider effect", "unclassified")
        consumed = self.paths.permit_consumed(attempt.source_sequence_id)
        if not consumed.exists():
            return Uncertain("consumed_permit_absent", "no provider effect", "unclassified")
        permit = EffectPermit.model_validate_json(consumed.read_bytes())
        if (
            permit.intent_id != attempt.intent_id
            or hashlib.sha256(effect_payload).hexdigest() != permit.effect_sha256
        ):
            return Uncertain(
                "consumed_permit_binding_mismatch", "no provider effect", "unclassified"
            )
        effect = EquityPaperEffect.model_validate_json(effect_payload)
        try:
            try:
                self.validate_effect(effect_payload, self._current, permit)
            except SupervisedRefused as refusal:
                return Uncertain(
                    "preflight_refused", f"no provider effect; {refusal.reason}", "unclassified"
                )
            effect_at = self.clock()
            claim = SupervisedIntent.model_validate(
                {
                    k: v
                    for k, v in json.loads(
                        self.paths.sending(attempt.intent_id).read_text()
                    ).items()
                    if k in SupervisedIntent.model_fields
                }
            )
            if effect_at >= permit.expires_at or effect_at >= claim.send_deadline:
                return Uncertain("effect_deadline_expired", "no provider effect", "unclassified")
            observed = self.provider._submit(
                symbol=effect.symbol,
                side=effect.side,
                quantity=effect.quantity,
                limit=effect.limit,
                client_order_id=stable_client_order_id(attempt.intent_id),
            )
            snap = snapshot_from_mapping(observed.body, request_id=observed.request_id)
            if snap.brokerage_order_id:
                self.bind_broker_order(snap.brokerage_order_id, attempt.intent_id)
            readback = readback_from_snapshot(
                self._current, snap, locally_received_at=observed.captured_at
            )
            if readback.status.value == "REJECTED":
                return Refused(
                    rejection_from_snapshot(
                        self._current, snap, locally_received_at=observed.captured_at
                    )
                )
            acknowledgement = acknowledgement_from_snapshot(
                self._current, snap, locally_received_at=observed.captured_at
            )
            return Acknowledged(acknowledgement, (readback,))
        except TimeoutError:
            return Uncertain("provider_timeout", "readback required; no resubmit")
        except ProviderDisconnected:
            return Uncertain("provider_disconnect", "readback required; no resubmit", "disconnect")
        except Exception:
            return Uncertain(
                "provider_facts_unrepresentable", "readback required; no resubmit", "unclassified"
            )

    def lookup(self, intent_id: str) -> Submitted | LookupUnknown:
        try:
            lifecycle = project_intent(self.paths, intent_id)
            order = lifecycle.intent
            broker_ids: set[str] = {
                str(broker_id)
                for r in lifecycle.records
                if (broker_id := getattr(r, "broker_order_id", None))
            }
            if len(broker_ids) > 1:
                return LookupUnknown("broker_order_identity_conflict")
            if broker_ids:
                observed = self.provider.order_readback(next(iter(broker_ids)))
                rows = [observed.body]
            else:
                account = self.provider.account_snapshot()
                observed = account.orders
                rows = [
                    r
                    for r in observed.body
                    if r.get("client_order_id") == stable_client_order_id(intent_id)
                ]
            if len(rows) != 1:
                return LookupUnknown(
                    "missing_or_nonunique_order; absence does not prove not-submitted"
                )
            snap = snapshot_from_mapping(rows[0], request_id=observed.digest)
            fact = readback_from_snapshot(order, snap, locally_received_at=observed.captured_at)
            if not snap.brokerage_order_id or fact.status.value == "AMBIGUOUS":
                return LookupUnknown("ambiguous_order_state")
            existing = next(
                (r for r in lifecycle.records if getattr(r, "record_id", None) == fact.record_id),
                None,
            )
            if existing is not None:
                if not isinstance(existing, BrokerReadback):
                    return LookupUnknown("provider_identity_type_collision")
                if existing.model_dump(exclude={"locally_received_at"}) != fact.model_dump(
                    exclude={"locally_received_at"}
                ):
                    return LookupUnknown("provider_identity_collision")
                fact = existing
            self.bind_broker_order(snap.brokerage_order_id, intent_id)
            return Submitted(snap.brokerage_order_id, (fact,))
        except SupervisedRefused as refusal:
            return LookupUnknown(refusal.reason)
        except Exception:
            return LookupUnknown("readback_unavailable_or_unrepresentable")

    def _graph(self, operation: str, intent_id: str, refs: dict[str, Any]) -> None:
        _append_line(
            self.paths.root / "provenance.jsonl",
            {
                "operation": operation,
                "intent_id": intent_id,
                "at": self.clock().isoformat(),
                "refs": refs,
                "environment": "broker_paper",
            },
        )

    def publish(self) -> None:
        _atomic_write(self.paths.root / "projection.json", self.projection())

    def halt(self) -> None:
        revoke_mandate(self.paths, now=self.clock(), reason="operator_halt")
        self.ready = False

    def projection(self) -> dict[str, Any]:
        executions = []
        for path in sorted((self.paths.root / "journal").glob("*.jsonl")):
            if path.name.endswith(".reconcile.jsonl"):
                continue
            lifecycle = project_intent(self.paths, path.stem)
            report = reconcile(lifecycle)
            receipt = assess_evidence(lifecycle)
            executions.append(
                {
                    "intent_id": lifecycle.intent.intent_id,
                    "state": lifecycle.state.value,
                    "broker_state": lifecycle.broker_state.value,
                    "reconciliation_clean": report.is_clean,
                    "findings": [f.reason.value for f in report.findings],
                    "evidence_verdict": receipt.verdict.value,
                    "exact_economics": receipt.is_admissible,
                    "records": [r.model_dump(mode="json") for r in lifecycle.records],
                }
            )
        return {
            "observed_at": self.clock().isoformat(),
            "mandate": supervised_status(self.paths, now=self.clock())["mandate"],
            "risk": self._screening,
            "environment": "BROKER PAPER",
            "account_alias": self.provider.binding.alias,
            "owner_epoch": self.owner.epoch,
            "owner_held": self.owner.held,
            "ready": self.ready,
            "live_money": False,
            "executions": executions,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="TREX SnapTrade paper read-only/recovery controls")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("command", choices=("read-only", "recover", "status", "halt", "run"))
    args = parser.parse_args()
    paths = SupervisedPaths(args.state)
    if args.command == "halt":
        revoke_mandate(paths, now=datetime.now(UTC), reason="operator_halt")
        print("Paper mandate revoked; existing broker orders require reconciliation.")
        return 0
    provider = SnapTradeProvider.from_private_file(args.config)
    runtime = SnapTradePaperRuntime(provider, paths)
    try:
        runtime.start()
        runtime.publish()
        if args.command == "run":
            import signal
            import threading

            stop = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            while not stop.wait(15):
                try:
                    runtime.refresh()
                except Exception:
                    runtime.ready = False
                runtime.publish()
        print(json.dumps(runtime.projection(), sort_keys=True))
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
