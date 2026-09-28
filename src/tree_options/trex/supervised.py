"""Supervised paper-path authority: mandate, permit, durable intent outbox.

This module is the operator-facing authority layer that the proposal
surface (``tree_options.action_graph``) declares and the broker-neutral
execution contracts (``tree_options.execution``) project. It composes
both with three crash-safe mechanisms:

- an expiring, account-bound, strategy-scoped **mandate** granted by the
  operator; at most one is active and nothing here can grant itself;
- a single-use **effect permit** issued only after the pure canary
  screening returns zero blockers, bound to the exact effect bytes it
  may submit (``effect_hash_bound``) and never outliving its intent's
  send deadline;
- a durable **intent outbox** (pending -> sending -> terminal) plus an
  append-only per-intent execution journal.

Sending travels an injected ``SupervisedBroker`` port; this module never
contacts a broker. On any uncertain outcome the effect is preserved
(``preserve_uncertain_effect``) and only ``reconcile_intent`` can clear
it: a retry requires a ``confirmed_not_submitted`` verdict first
(``reconcile_before_retry``), expressed structurally because a terminal
uncertain intent can never re-enter ``send`` and a new intent for the
same package is refused while the old one is unresolved. Reconciliation
waits out a settle window, so a lookup that races a just-sent order
cannot clear it; confirmed verdicts never change, while an inconclusive
lookup is only audited and may be retried.

Every mutating operation holds one advisory lock on the state directory,
so a CLI and a runtime cannot both spend the same mandate budget.
Broker facts are folded through the pure lifecycle BEFORE they are
journaled; facts the lifecycle refuses turn the outcome uncertain rather
than poisoning the journal.

All instants are timezone-aware; ages and TTLs are epoch-second math
(the repo bans naive ``timedelta`` construction outside ``time/``).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Protocol

from pydantic import Field, TypeAdapter

from tree_options.action_graph.proposal import canonical_bytes
from tree_options.execution import (
    BrokerAcknowledgement,
    ExecutionLifecycle,
    ExecutionLifecycleError,
    ExecutionRecord,
    OrderIntent,
    OrderReject,
    SubmitAttempt,
    TimeoutObserved,
)
from tree_options.execution.records import StoredExecutionRecord
from tree_options.schemas.common import IdStr, StrictModel
from tree_options.time.sessions import shift_instant

MANDATE_SCHEMA = "supervised-mandate/1"
INTENT_SCHEMA = "supervised-intent/1"
PERMIT_SCHEMA = "supervised-permit/1"

#: Operator rulings bound the mandate lifetime. Ruling 2026-09-28: multi-day
#: grants are allowed (a long-running desk survives gateway restarts without
#: re-granting); the original 8h ceiling became the LONG_GRANT threshold —
#: beyond it the mandate is flagged long_running and status shows days left,
#: so a forgotten grant stays visible.
MIN_MANDATE_TTL_S = 60
MAX_MANDATE_TTL_S = 7 * 24 * 60 * 60
LONG_GRANT_S = 8 * 60 * 60

#: A permit lives only inside its intent's send deadline and this bound.
MAX_PERMIT_TTL_S = 15 * 60

#: An uncertain effect cannot be reconciled sooner than this after its
#: terminal receipt: a lookup racing a just-sent order must not clear it.
RECONCILE_SETTLE_S = 120

#: How long a mutating call waits for the state lock before refusing.
LOCK_WAIT_S = 10.0

Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SupervisedRefused(RuntimeError):
    """Fail-closed refusal with a machine-readable reason."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _require_aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _reached(now: datetime, instant: datetime) -> bool:
    return (now - instant).total_seconds() >= 0


class SupervisedMandate(StrictModel):
    """Operator-granted, expiring, account-bound trading authority."""

    schema_version: str = Field(default=MANDATE_SCHEMA, alias="schema")
    mandate_id: IdStr
    environment: str = "ibkr-paper"
    account_id: IdStr
    owner_epoch: IdStr
    strategy_version: IdStr
    profile_digest: Sha256Hex
    max_orders: int = Field(strict=True, ge=1)
    orders_used: int = Field(strict=True, ge=0, default=0)
    granted_at: datetime
    expires_at: datetime
    granted_by: IdStr
    #: set by grant_mandate when the grant outlives a session day; a plain
    #: field (not derived) so the persisted record says what was ruled
    long_running: bool | None = None

    def model_post_init(self, __context: Any) -> None:
        _require_aware(self.granted_at, "granted_at")
        _require_aware(self.expires_at, "expires_at")
        if self.expires_at <= self.granted_at:
            raise ValueError("expires_at must be after granted_at")
        if self.orders_used > self.max_orders:
            raise ValueError("orders_used cannot exceed max_orders")
        if self.environment != "ibkr-paper":
            raise ValueError("this build supervises the paper environment only")

    def expired_at(self, now: datetime) -> bool:
        return _reached(now, self.expires_at)

    def days_left(self, now: datetime) -> int:
        """Whole days of authority remaining (>= 0)."""
        return max(0, int((self.expires_at - now).total_seconds() // 86400))


class SupervisedIntent(StrictModel):
    """Durable order intent with its canary package binding and deadline."""

    schema_version: str = Field(default=INTENT_SCHEMA, alias="schema")
    intent: OrderIntent
    package_intent_sha256: Sha256Hex
    created_at: datetime
    send_deadline: datetime

    def model_post_init(self, __context: Any) -> None:
        _require_aware(self.created_at, "created_at")
        _require_aware(self.send_deadline, "send_deadline")
        if self.send_deadline <= self.created_at:
            raise ValueError("send_deadline must be after created_at")


class EffectPermit(StrictModel):
    """Single-use submit authority bound to exact effect bytes."""

    schema_version: str = Field(default=PERMIT_SCHEMA, alias="schema")
    permit_id: IdStr
    mandate_id: IdStr
    intent_id: IdStr
    package_intent_sha256: Sha256Hex
    effect_sha256: Sha256Hex
    account_id: IdStr
    owner_epoch: IdStr
    strategy_version: IdStr
    screening_sha256: Sha256Hex
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None

    def model_post_init(self, __context: Any) -> None:
        _require_aware(self.issued_at, "issued_at")
        _require_aware(self.expires_at, "expires_at")
        if self.consumed_at is not None:
            _require_aware(self.consumed_at, "consumed_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")


@dataclass(frozen=True)
class Acknowledged:
    """A submit the broker answered with an order identity and facts."""

    acknowledgement: BrokerAcknowledgement
    facts: tuple[ExecutionRecord, ...] = ()


@dataclass(frozen=True)
class Refused:
    """A submit the broker answered with a rejection."""

    reject: OrderReject


@dataclass(frozen=True)
class Uncertain:
    """A submit whose outcome is unknown; the effect is preserved."""

    reason: str
    detail: str = ""


type SubmissionOutcome = Acknowledged | Refused | Uncertain

_RECORD_ADAPTER: TypeAdapter[ExecutionRecord] = TypeAdapter(ExecutionRecord)


@dataclass(frozen=True)
class NotSubmitted:
    """Positive evidence that no broker order exists for the intent.

    An adapter returns this only from a connected, authoritative view
    (open AND completed orders for the bound account, matched by the
    intent's order tag); absence of evidence is ``LookupUnknown``.
    """


@dataclass(frozen=True)
class Submitted:
    """Evidence of a broker order for the intent.

    ``facts`` should carry the acknowledgement and any fills/readbacks so
    the journal projects the order's real state.
    """

    broker_order_id: str
    facts: tuple[ExecutionRecord, ...] = ()


@dataclass(frozen=True)
class LookupUnknown:
    """Reconciliation could not determine submission either way."""

    reason: str


LookupVerdict = NotSubmitted | Submitted | LookupUnknown


class SupervisedBroker(Protocol):
    """The only path to broker contact; injected, never constructed here.

    ``submit`` receives the exact effect bytes whose hash the permit bound,
    so an adapter builds its order FROM the verified bytes: what was
    checked is what is sent.
    """

    def submit(self, attempt: SubmitAttempt, effect_payload: bytes) -> SubmissionOutcome: ...

    def lookup(self, intent_id: str) -> LookupVerdict: ...


class _OutboxState(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    RECEIPT = "receipt"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"
    RECONCILED = "reconciled"


@dataclass(frozen=True)
class SupervisedPaths:
    """All durable state under one root; nothing else is touched."""

    root: Path

    @classmethod
    def default(cls) -> SupervisedPaths:
        env = os.environ.get("TREX_SUPERVISED_DIR", "~/.local/state/trex/supervised")
        return cls(Path(env).expanduser())

    def prepare(self) -> None:
        for sub in ("permits", "outbox", "journal"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def lock(self) -> Path:
        return self.root / ".lock"

    def mandate(self) -> Path:
        return self.root / "mandate.json"

    def mandate_revoked(self) -> Path:
        return self.root / "mandate.revoked.json"

    def permit(self, permit_id: str) -> Path:
        return self.root / "permits" / f"{permit_id}.issued.json"

    def permit_consumed(self, permit_id: str) -> Path:
        return self.root / "permits" / f"{permit_id}.consumed.json"

    def outbox_dir(self) -> Path:
        return self.root / "outbox"

    def pending(self, intent_id: str) -> Path:
        return self.outbox_dir() / f"{intent_id}.pending.json"

    def sending(self, intent_id: str) -> Path:
        return self.outbox_dir() / f"{intent_id}.sending.json"

    def terminal(self, intent_id: str) -> Path:
        return self.outbox_dir() / f"{intent_id}.terminal.json"

    def reconciled(self, intent_id: str) -> Path:
        return self.outbox_dir() / f"{intent_id}.reconciled.json"

    def journal(self, intent_id: str) -> Path:
        return self.root / "journal" / f"{intent_id}.jsonl"

    def reconcile_audit(self, intent_id: str) -> Path:
        return self.root / "journal" / f"{intent_id}.reconcile.jsonl"


@contextlib.contextmanager
def _locked(paths: SupervisedPaths) -> Iterator[None]:
    """Hold the state directory's advisory lock, or refuse ``state_busy``."""
    paths.prepare()
    with open(paths.lock(), "a+b") as handle:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SupervisedRefused("state_busy", str(paths.lock())) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    with open(tmp, "wb") as stream:
        stream.write(canonical_bytes(payload))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _durable_rename(source: Path, target: Path) -> None:
    os.rename(source, target)
    _fsync_dir(target.parent)


def _append_line(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(canonical_bytes(payload).decode("utf-8"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _read_model(path: Path) -> dict[str, Any]:
    return json.loads(path.read_bytes())


def _dump(model: StrictModel) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True)


# --------------------------------------------------------------- mandate


def grant_mandate(paths: SupervisedPaths, *, now: datetime, account_id: str,
                  owner_epoch: str, strategy_version: str, profile_digest: str,
                  max_orders: int, ttl_seconds: int, granted_by: str,
                  mandate_id: str | None = None) -> SupervisedMandate:
    """Grant a new mandate; an unexpired one must be revoked or expire first."""
    _require_aware(now, "now")
    if not MIN_MANDATE_TTL_S <= ttl_seconds <= MAX_MANDATE_TTL_S:
        raise SupervisedRefused(
            "mandate_ttl_out_of_bounds",
            f"ttl {ttl_seconds}s outside [{MIN_MANDATE_TTL_S}, {MAX_MANDATE_TTL_S}]")
    with _locked(paths):
        if paths.mandate_revoked().exists():
            raise SupervisedRefused("mandate_revoked_permanent",
                                    "remove the tombstone by hand to re-arm")
        if paths.mandate().exists():
            existing = SupervisedMandate.model_validate(_read_model(paths.mandate()))
            if not existing.expired_at(now):
                raise SupervisedRefused("mandate_already_active", existing.mandate_id)
            _durable_rename(paths.mandate(),
                            paths.root / f"mandate.expired-{existing.mandate_id}.json")
        mandate = SupervisedMandate(
            mandate_id=mandate_id or f"mandate-{int(now.timestamp()):x}",
            account_id=account_id, owner_epoch=owner_epoch,
            strategy_version=strategy_version, profile_digest=profile_digest,
            max_orders=max_orders, granted_at=now,
            expires_at=shift_instant(now, ttl_seconds), granted_by=granted_by,
            long_running=ttl_seconds > LONG_GRANT_S)
        _atomic_write(paths.mandate(), _dump(mandate))
        return mandate


def revoke_mandate(paths: SupervisedPaths, *, now: datetime, reason: str) -> None:
    """Replace the mandate with a permanent tombstone."""
    _require_aware(now, "now")
    with _locked(paths):
        if not paths.mandate().exists():
            raise SupervisedRefused("mandate_absent")
        mandate = SupervisedMandate.model_validate(_read_model(paths.mandate()))
        record = _dump(mandate)
        record["revoked_at"] = now.isoformat()
        record["revoked_reason"] = reason
        _atomic_write(paths.mandate_revoked(), record)
        paths.mandate().unlink()
        _fsync_dir(paths.root)


def active_mandate(paths: SupervisedPaths, *, now: datetime, account_id: str,
                   owner_epoch: str, strategy_version: str) -> SupervisedMandate:
    """Load the one mandate and refuse, with the reason, on any mismatch."""
    _require_aware(now, "now")
    if paths.mandate_revoked().exists():
        raise SupervisedRefused("mandate_revoked")
    if not paths.mandate().exists():
        raise SupervisedRefused("mandate_absent")
    mandate = SupervisedMandate.model_validate(_read_model(paths.mandate()))
    if mandate.expired_at(now):
        raise SupervisedRefused("mandate_expired", mandate.mandate_id)
    if mandate.account_id != account_id:
        raise SupervisedRefused("mandate_account_mismatch")
    if mandate.owner_epoch != owner_epoch:
        raise SupervisedRefused("mandate_owner_mismatch")
    if mandate.strategy_version != strategy_version:
        raise SupervisedRefused("mandate_scope_mismatch")
    return mandate


# ---------------------------------------------------------------- intent


def record_intent(paths: SupervisedPaths, intent: SupervisedIntent) -> Path:
    """Persist a pending intent; identical replays are idempotent."""
    intent_id = intent.intent.intent_id
    with _locked(paths):
        path = paths.pending(intent_id)
        if path.exists():
            existing = SupervisedIntent.model_validate(_read_model(path))
            if _dump(existing) != _dump(intent):
                raise SupervisedRefused("intent_id_collision", intent_id)
            return path
        for used in (paths.sending(intent_id), paths.terminal(intent_id),
                     paths.reconciled(intent_id)):
            if used.exists():
                raise SupervisedRefused("intent_id_reused", used.name)
        # the new intent's creation time is the reference "now"
        blocked = _in_flight_reason(paths, intent.package_intent_sha256, intent.created_at)
        if blocked is not None:
            raise SupervisedRefused(blocked[0], blocked[1])
        _atomic_write(path, _dump(intent))
        return path


_TERMINAL_STATES = {"acknowledged": _OutboxState.RECEIPT,
                    "rejected": _OutboxState.REJECTED,
                    "uncertain": _OutboxState.UNCERTAIN}


def _outbox_states(paths: SupervisedPaths) -> list[tuple[str, _OutboxState, dict[str, Any]]]:
    """Every outbox entry as (intent_id, state, document)."""
    found: list[tuple[str, _OutboxState, dict[str, Any]]] = []
    if not paths.outbox_dir().exists():
        return found
    for path in sorted(paths.outbox_dir().glob("*.json")):
        stripped = path.name.removesuffix(".json")
        state_name = stripped.rsplit(".", 1)[-1]
        document = _read_model(path)
        if state_name == "terminal":
            state = _TERMINAL_STATES.get(str(document.get("outcome")))
            if state is None:
                continue
        else:
            try:
                state = _OutboxState(state_name)
            except ValueError:
                continue
        stem = stripped[: -(len(state_name) + 1)]
        found.append((stem, state, document))
    return found


def _in_flight_reason(paths: SupervisedPaths, package_sha: str,
                      now: datetime) -> tuple[str, str] | None:
    """The refusal for a NEW intent on a package that is not cleared.

    A PENDING intent past its send deadline is not in flight: ``send``
    refuses it, so it can never reach the broker."""
    verdicts: dict[str, str] = {}
    entries: dict[str, _OutboxState] = {}
    for intent_id, state, document in _outbox_states(paths):
        if document.get("package_intent_sha256") != package_sha:
            continue
        if state == _OutboxState.PENDING:
            deadline = datetime.fromisoformat(str(document["send_deadline"]))
            if _reached(now, deadline):
                continue
        if state == _OutboxState.RECONCILED:
            verdicts[intent_id] = str(document.get("verdict", ""))
        else:
            entries[intent_id] = state
    for intent_id, verdict in sorted(verdicts.items()):
        if verdict != "confirmed_not_submitted":
            return ("package_already_in_flight", f"{intent_id}:reconciled:{verdict}")
    for intent_id, state in sorted(entries.items()):
        if intent_id in verdicts:
            # A confirmed verdict supersedes the uncertain terminal it clears.
            continue
        if state in (_OutboxState.PENDING, _OutboxState.SENDING,
                     _OutboxState.RECEIPT, _OutboxState.UNCERTAIN):
            return ("package_already_in_flight", f"{intent_id}:{state.value}")
    return None


# ---------------------------------------------------------------- permit


def issue_permit(paths: SupervisedPaths, *, now: datetime,
                 account_id: str, owner_epoch: str,
                 intent: SupervisedIntent, canary_blockers: Sequence[str],
                 effect_payload: bytes, screening_sha256: str,
                 ttl_seconds: int = MAX_PERMIT_TTL_S) -> EffectPermit:
    """Spend one mandate order on a hash-bound permit, or refuse.

    Refusal reasons: canary blockers, a passed send deadline, an inactive
    or exhausted mandate, or a permit that already exists for the intent.
    The permit expires at the earlier of its TTL and the send deadline.
    """
    _require_aware(now, "now")
    if canary_blockers:
        raise SupervisedRefused("canary_blockers", ",".join(canary_blockers))
    if _reached(now, intent.send_deadline):
        raise SupervisedRefused("intent_deadline_passed")
    if not 1 <= ttl_seconds <= MAX_PERMIT_TTL_S:
        raise SupervisedRefused("permit_ttl_out_of_bounds")
    with _locked(paths):
        # The intent's emitting source must equal the granted strategy
        # scope; a mismatch refuses here as mandate_scope_mismatch.
        mandate = active_mandate(paths, now=now, account_id=account_id,
                                 owner_epoch=owner_epoch,
                                 strategy_version=intent.intent.source)
        if mandate.orders_used >= mandate.max_orders:
            raise SupervisedRefused("mandate_budget_exhausted")
        for path in sorted((paths.root / "permits").glob("*.json")):
            if _read_model(path).get("intent_id") == intent.intent.intent_id:
                raise SupervisedRefused("permit_already_issued", path.name)
        effect_sha = hashlib.sha256(effect_payload).hexdigest()
        permit_id = "permit-" + hashlib.sha256(
            canonical_bytes({"intent_id": intent.intent.intent_id,
                             "effect_sha256": effect_sha})).hexdigest()[:16]
        expires_at = min(shift_instant(now, ttl_seconds), intent.send_deadline)
        permit = EffectPermit(
            permit_id=permit_id, mandate_id=mandate.mandate_id,
            intent_id=intent.intent.intent_id,
            package_intent_sha256=intent.package_intent_sha256,
            effect_sha256=effect_sha, account_id=account_id, owner_epoch=owner_epoch,
            strategy_version=mandate.strategy_version, screening_sha256=screening_sha256,
            issued_at=now, expires_at=expires_at)
        # Budget first: a crash between the writes wastes an order, never
        # mints one.
        spent = mandate.model_copy(update={"orders_used": mandate.orders_used + 1})
        _atomic_write(paths.mandate(), _dump(spent))
        _atomic_write(paths.permit(permit_id), _dump(permit))
        return permit


# ----------------------------------------------------------------- send


def _journal_append(paths: SupervisedPaths, record: StoredExecutionRecord) -> None:
    _append_line(paths.journal(record.intent_id), _dump(record))


def load_journal(paths: SupervisedPaths, intent_id: str) -> list[StoredExecutionRecord]:
    """Replay the append-only journal as validated records."""
    path = paths.journal(intent_id)
    if not path.exists():
        return []
    records: list[StoredExecutionRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        document = json.loads(line)
        if document.get("record_type") == "ORDER_INTENT":
            records.append(OrderIntent.model_validate(document))
        else:
            records.append(_RECORD_ADAPTER.validate_python(document))
    return records


def _fold(lifecycle: ExecutionLifecycle,
          records: Sequence[ExecutionRecord]) -> ExecutionLifecycle:
    for record in records:
        lifecycle = lifecycle.apply(record)
    return lifecycle


def project_intent(paths: SupervisedPaths, intent_id: str) -> ExecutionLifecycle:
    """Fold the journal through the pure lifecycle projection."""
    records = load_journal(paths, intent_id)
    if not records or not isinstance(records[0], OrderIntent):
        raise SupervisedRefused("journal_empty", intent_id)
    tail = [r for r in records[1:] if not isinstance(r, OrderIntent)]
    return _fold(ExecutionLifecycle.start(records[0]), tail)


def _uncertain_receipt(permit: EffectPermit, now: datetime, reason: str,
                       detail: str) -> dict[str, Any]:
    return {"schema": "supervised-send-receipt/1", "intent_id": permit.intent_id,
            "outcome": "uncertain", "reason": reason, "detail": detail,
            "package_intent_sha256": permit.package_intent_sha256,
            "permit_id": permit.permit_id, "at": now.isoformat()}


def send(paths: SupervisedPaths, *, now: datetime, permit_id: str,
         effect_payload: bytes, broker: SupervisedBroker) -> dict[str, Any]:
    """The send boundary: guards, durable claim, consume, submit, receipt.

    Order matters and is load-bearing: the sending claim and the permit
    consumption are persisted BEFORE the broker call, so a crash at any
    point leaves an uncertain effect that only reconciliation can clear.
    """
    _require_aware(now, "now")
    with _locked(paths):
        issued = paths.permit(permit_id)
        consumed = paths.permit_consumed(permit_id)
        if not issued.exists():
            if consumed.exists():
                raise SupervisedRefused("permit_consumed", permit_id)
            raise SupervisedRefused("permit_absent", permit_id)
        permit = EffectPermit.model_validate(_read_model(issued))
        if _reached(now, permit.expires_at):
            raise SupervisedRefused("permit_expired", permit_id)
        if hashlib.sha256(effect_payload).hexdigest() != permit.effect_sha256:
            raise SupervisedRefused("effect_hash_mismatch", permit_id)
        active_mandate(paths, now=now, account_id=permit.account_id,
                       owner_epoch=permit.owner_epoch,
                       strategy_version=permit.strategy_version)
        pending = paths.pending(permit.intent_id)
        if not pending.exists():
            if paths.terminal(permit.intent_id).exists() or paths.reconciled(permit.intent_id).exists():
                raise SupervisedRefused("intent_terminal", permit.intent_id)
            if paths.sending(permit.intent_id).exists():
                raise SupervisedRefused("intent_in_flight", permit.intent_id)
            raise SupervisedRefused("intent_absent", permit.intent_id)
        intent = SupervisedIntent.model_validate(_read_model(pending))
        if intent.package_intent_sha256 != permit.package_intent_sha256:
            raise SupervisedRefused("intent_permit_binding_mismatch", permit.intent_id)
        if _reached(now, intent.send_deadline):
            raise SupervisedRefused("intent_deadline_passed", permit.intent_id)
        # Durable claim before any broker contact: the rename IS the claim.
        _durable_rename(pending, paths.sending(permit.intent_id))
        claim = _dump(intent)
        claim["claimed_at"] = now.isoformat()
        claim["permit_id"] = permit_id
        claim["effect_sha256"] = permit.effect_sha256
        _atomic_write(paths.sending(permit.intent_id), claim)
        # Consume the permit before submitting: the rename IS the consumption.
        _durable_rename(issued, consumed)
        consumed_doc = _dump(permit)
        consumed_doc["consumed_at"] = now.isoformat()
        _atomic_write(consumed, consumed_doc)
        attempt = SubmitAttempt(
            record_id=f"sup-send-{permit_id}", intent_id=permit.intent_id,
            send_attempt_at=now, source="supervised", source_sequence_id=permit_id)
        _journal_append(paths, intent.intent)
        _journal_append(paths, attempt)
        base = _fold(ExecutionLifecycle.start(intent.intent), [attempt])
        outcome: SubmissionOutcome
        try:
            outcome = broker.submit(attempt, effect_payload)
        except Exception as error:  # preserved as an uncertain effect, never swallowed
            outcome = Uncertain("broker_transport_error", repr(error))
        facts: list[ExecutionRecord] = []
        if isinstance(outcome, Acknowledged):
            facts = [outcome.acknowledgement, *outcome.facts]
        elif isinstance(outcome, Refused):
            facts = [outcome.reject]
        if facts:
            try:
                _fold(base, facts)
            except ExecutionLifecycleError as error:
                outcome = Uncertain("broker_facts_rejected", repr(error))
                facts = []
        for fact in facts:
            _journal_append(paths, fact)
        receipt: dict[str, Any]
        if isinstance(outcome, Acknowledged):
            receipt = {"schema": "supervised-send-receipt/1", "intent_id": permit.intent_id,
                       "outcome": "acknowledged",
                       "broker_order_id": outcome.acknowledgement.broker_order_id,
                       "package_intent_sha256": permit.package_intent_sha256,
                       "permit_id": permit_id, "at": now.isoformat()}
        elif isinstance(outcome, Refused):
            receipt = {"schema": "supervised-send-receipt/1", "intent_id": permit.intent_id,
                       "outcome": "rejected", "reason_code": outcome.reject.reason_code,
                       "package_intent_sha256": permit.package_intent_sha256,
                       "permit_id": permit_id, "at": now.isoformat()}
        else:
            _journal_append(paths, TimeoutObserved(
                record_id=f"sup-timeout-{permit_id}", intent_id=permit.intent_id,
                attempt_id=attempt.record_id, locally_received_at=now,
                source="supervised", source_sequence_id=f"timeout-{permit_id}"))
            receipt = _uncertain_receipt(permit, now, outcome.reason, outcome.detail)
        _atomic_write(paths.terminal(permit.intent_id), receipt)
        paths.sending(permit.intent_id).unlink(missing_ok=True)
        _fsync_dir(paths.outbox_dir())
        return receipt


# ------------------------------------------------------------ reconcile


def reconcile_intent(paths: SupervisedPaths, *, now: datetime, intent_id: str,
                     broker: SupervisedBroker) -> dict[str, Any]:
    """Clear an uncertain effect with broker evidence, or keep it held.

    Confirmed verdicts are persisted once and never change; a conflicting
    later confirmation is refused. An inconclusive lookup is appended to
    the intent's reconcile audit only, so the package stays held and the
    reconciliation can be retried.
    """
    _require_aware(now, "now")
    with _locked(paths):
        _recover(paths, now=now)
        terminal_path = paths.terminal(intent_id)
        if not terminal_path.exists():
            raise SupervisedRefused("intent_not_terminal", intent_id)
        terminal = _read_model(terminal_path)
        if terminal.get("outcome") != "uncertain":
            raise SupervisedRefused("intent_not_uncertain", intent_id)
        settle_at = shift_instant(datetime.fromisoformat(str(terminal["at"])),
                                  RECONCILE_SETTLE_S)
        if not _reached(now, settle_at):
            raise SupervisedRefused("reconcile_too_early", f"settles at {settle_at.isoformat()}")
        prior_path = paths.reconciled(intent_id)
        prior = _read_model(prior_path) if prior_path.exists() else None
        verdict = broker.lookup(intent_id)
        detail = ""
        facts: list[ExecutionRecord] = []
        if isinstance(verdict, NotSubmitted):
            outcome = "confirmed_not_submitted"
        elif isinstance(verdict, Submitted):
            outcome = "confirmed_submitted"
            facts = list(verdict.facts)
            try:
                _fold(project_intent(paths, intent_id), facts)
            except (ExecutionLifecycleError, SupervisedRefused) as error:
                outcome, detail, facts = "still_uncertain", f"facts_rejected: {error!r}", []
        else:
            outcome, detail = "still_uncertain", verdict.reason
        record = {"schema": "supervised-reconciliation/1", "intent_id": intent_id,
                  "verdict": outcome, "detail": detail, "at": now.isoformat(),
                  "package_intent_sha256": terminal.get("package_intent_sha256"),
                  "broker_order_id": getattr(verdict, "broker_order_id", None)}
        if outcome == "still_uncertain":
            _append_line(paths.reconcile_audit(intent_id), record)
            return {**record, "persisted": False}
        if prior is not None:
            if prior.get("verdict") != outcome:
                raise SupervisedRefused("reconcile_verdict_conflict",
                                        f"{prior.get('verdict')} -> {outcome}")
            return prior
        for fact in facts:
            _journal_append(paths, fact)
        _append_line(paths.reconcile_audit(intent_id), record)
        _atomic_write(prior_path, record)
        return record


def _recover(paths: SupervisedPaths, *, now: datetime) -> list[str]:
    recovered: list[str] = []
    if not paths.outbox_dir().exists():
        return recovered
    for path in sorted(paths.outbox_dir().glob("*.sending.json")):
        intent_id = path.name.removesuffix(".sending.json")
        if paths.terminal(intent_id).exists():
            path.unlink(missing_ok=True)
            continue
        try:
            package_sha = _read_model(path).get("package_intent_sha256")
        except (OSError, ValueError):
            package_sha = None
        receipt = {"schema": "supervised-send-receipt/1", "intent_id": intent_id,
                   "outcome": "uncertain", "reason": "sending_orphan_recovered",
                   "detail": "process died between claim and terminal write",
                   "package_intent_sha256": package_sha,
                   "at": now.isoformat()}
        _atomic_write(paths.terminal(intent_id), receipt)
        path.unlink()
        recovered.append(intent_id)
    return recovered


def recover(paths: SupervisedPaths, *, now: datetime) -> list[str]:
    """Reclassify orphan sending claims as uncertain; idempotent."""
    _require_aware(now, "now")
    with _locked(paths):
        return _recover(paths, now=now)


def status(paths: SupervisedPaths, *, now: datetime) -> dict[str, Any]:
    """Read-only inventory for the operator (never raises on bad journals)."""
    _require_aware(now, "now")
    report: dict[str, Any] = {"schema": "supervised-status/1", "at": now.isoformat()}
    if paths.mandate_revoked().exists():
        report["mandate"] = {"state": "revoked"}
    elif paths.mandate().exists():
        mandate = SupervisedMandate.model_validate(_read_model(paths.mandate()))
        report["mandate"] = {"state": "expired" if mandate.expired_at(now) else "active",
                             "days_left": mandate.days_left(now),
                             **_dump(mandate)}
    else:
        report["mandate"] = {"state": "absent"}
    entries = []
    for intent_id, state, document in _outbox_states(paths):
        entry: dict[str, Any] = {"intent_id": intent_id, "state": state.value}
        if state == _OutboxState.PENDING:
            entry["package_intent_sha256"] = document.get("package_intent_sha256")
        elif state == _OutboxState.RECONCILED:
            entry["verdict"] = document.get("verdict")
        elif state != _OutboxState.SENDING:
            entry["outcome"] = document.get("outcome")
        try:
            entry["execution_state"] = project_intent(paths, intent_id).state.value
        except SupervisedRefused:
            entry["execution_state"] = None
        except (ExecutionLifecycleError, ValueError) as error:
            entry["execution_state"] = "projection_error"
            entry["projection_error"] = repr(error)
        entries.append(entry)
    report["outbox"] = entries
    return report


# ------------------------------------------------------------------ CLI


def _cli() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tree_options.trex.supervised",
        description="Operator surface for the supervised paper path (no broker contact).")
    parser.add_argument("--dir", type=Path, default=None,
                        help="state directory (default TREX_SUPERVISED_DIR)")
    from datetime import UTC
    now = datetime.now(UTC)
    sub = parser.add_subparsers(dest="command", required=True)
    grant = sub.add_parser("grant", help="grant an expiring mandate")
    grant.add_argument("--account", required=True)
    grant.add_argument("--owner-epoch", required=True)
    grant.add_argument("--strategy", required=True)
    grant.add_argument("--profile-digest", required=True)
    grant.add_argument("--max-orders", type=int, default=1)
    grant.add_argument("--ttl-seconds", type=int, default=3600)
    grant.add_argument("--granted-by", required=True)
    sub.add_parser("revoke", help="revoke the active mandate").add_argument("--reason", default="")
    sub.add_parser("status", help="read-only inventory")
    sub.add_parser("recover", help="reclassify orphan sending claims")
    args = parser.parse_args()
    paths = SupervisedPaths(args.dir.expanduser() if args.dir else SupervisedPaths.default().root)
    try:
        if args.command == "grant":
            mandate = grant_mandate(
                paths, now=now, account_id=args.account, owner_epoch=args.owner_epoch,
                strategy_version=args.strategy, profile_digest=args.profile_digest,
                max_orders=args.max_orders, ttl_seconds=args.ttl_seconds,
                granted_by=args.granted_by)
            print(json.dumps(_dump(mandate), indent=2, sort_keys=True))
        elif args.command == "revoke":
            revoke_mandate(paths, now=now, reason=args.reason)
            print("revoked")
        elif args.command == "recover":
            print(json.dumps(recover(paths, now=now)))
        else:
            print(json.dumps(status(paths, now=now), indent=2, sort_keys=True))
    except SupervisedRefused as refused:
        print(f"refused: {refused.reason} {refused.detail}", file=sys.stderr)
        return 2
    except ValueError as invalid:
        print(f"refused: invalid_input {invalid}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
