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

Wave 2 adds per-trade authority (``supervised-mandate/2``, C2.3): one owner
approval (a validated ``approve_entry`` command) grants
``mandates/<intent_id>.json`` for exactly one order of exactly one effect
(``grant_entry_mandate``); ``issue_permit`` mints that intent's permit from
it, consuming it under the same lock by renaming it to
``<intent_id>.consumed.json``. An intent with a per-trade mandate never
falls back to the v1 single mandate, and an equity effect
(``supervised-equity-effect/1``) is only ever authorized per trade; the v1
``mandate.json`` path is otherwise unchanged (the canary path).

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
import re
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Protocol

from pydantic import Field, TypeAdapter

from tree_options.action_graph.proposal import canonical_bytes
from tree_options.execution import (
    BrokerAcknowledgement,
    DisconnectObserved,
    ExecutionLifecycle,
    ExecutionLifecycleError,
    ExecutionRecord,
    OrderIntent,
    OrderReject,
    SubmitAttempt,
    TimeoutObserved,
    UncertaintyObserved,
)
from tree_options.execution.records import StoredExecutionRecord
from tree_options.schemas.common import IdStr, StrictModel
from tree_options.time.sessions import shift_instant

if TYPE_CHECKING:
    # The Wave 2 contracts import this module (``contracts.canonical``), so
    # they are imported lazily at call time, never at module load.
    from tree_options.trex.contracts.commands import ApproveEntryCommand
    from tree_options.trex.contracts.mandates import SupervisedMandateV2
    from tree_options.trex.contracts.proposals import EntryProposal

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

#: A permit minted from a ``supervised-mandate/2`` names it
#: ``mandate2-<approval_id>``; the send boundary re-checks that mandate.
ENTRY_MANDATE_ID_PREFIX = "mandate2-"

#: Intent ids a per-trade mandate file may carry (the contracts'
#: ``PathSafeId``). ``.consumed`` is the consumed-state suffix, so an id
#: ending in it would name another intent's consumed file.
_ENTRY_INTENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
_CONSUMED_SUFFIX = ".consumed"

Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SupervisedRefused(RuntimeError):
    """Fail-closed refusal with a machine-readable reason."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class ApprovalRefused(SupervisedRefused):
    """A per-trade approval or permit refused with Wave 2 ``Blocker`` codes.

    ``blockers`` are the C2.7 wire strings in the contract's check order;
    ``reason`` is the first of them.
    """

    def __init__(self, blockers: Sequence[str], detail: str = "") -> None:
        codes = tuple(str(blocker) for blocker in blockers)
        if not codes:
            raise ValueError("an approval refusal names at least one blocker")
        super().__init__(codes[0], detail or ",".join(codes))
        self.blockers = codes


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
    max_gross_notional_usd: Decimal | None = None
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
        if self.environment not in {"ibkr-paper", "broker_paper"}:
            raise ValueError("this build supervises the paper environment only")
        if self.environment == "broker_paper":
            if self.max_orders != 1 or self.strategy_version != "operational-canary/1":
                raise ValueError("broker paper is bounded to one operational canary")
            if (
                self.max_gross_notional_usd is None
                or not self.max_gross_notional_usd.is_finite()
                or not Decimal("0") < self.max_gross_notional_usd <= Decimal("100")
            ):
                raise ValueError("broker paper requires an explicit notional cap at most 100 USD")
            if (self.expires_at - self.granted_at).total_seconds() > 900:
                raise ValueError("broker paper mandate TTL exceeds 900 seconds")

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
    observation: str = "timeout"


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

    # -- per-trade mandates (C2.3 / C2.6 ``Wave2SupervisedDir``) -----------

    def entry_mandates(self) -> Path:
        from tree_options.trex.contracts.layout import Wave2SupervisedDir

        return Wave2SupervisedDir(self.root).mandates()

    def entry_mandate(self, intent_id: str) -> Path:
        """A granted, unconsumed ``supervised-mandate/2``."""
        from tree_options.trex.contracts.layout import Wave2SupervisedDir

        return Wave2SupervisedDir(self.root).mandate(intent_id)

    def entry_mandate_consumed(self, intent_id: str) -> Path:
        """The same mandate after its permit was minted (rename = consumption)."""
        from tree_options.trex.contracts.layout import Wave2SupervisedDir

        return Wave2SupervisedDir(self.root).mandate_consumed(intent_id)

    def entry_mandate_retired(self, intent_id: str, approval_id: str) -> Path:
        """An unconsumed mandate that can no longer mint (expired, superseded,
        or from an earlier owner epoch); kept for the audit trail."""
        return self.entry_mandates() / "retired" / f"{intent_id}.{approval_id}.json"

    def entry_mandate_retirements(self) -> Path:
        return self.entry_mandates() / "retired" / "retired.jsonl"


@contextlib.contextmanager
def _locked(paths: SupervisedPaths, *, blocking: bool = True) -> Iterator[None]:
    """Hold the state directory's advisory lock, or refuse ``state_busy``."""
    paths.prepare()
    with open(paths.lock(), "a+b") as handle:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not blocking or time.monotonic() >= deadline:
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


def grant_mandate(
    paths: SupervisedPaths,
    *,
    now: datetime,
    account_id: str,
    owner_epoch: str,
    strategy_version: str,
    profile_digest: str,
    max_orders: int,
    ttl_seconds: int,
    granted_by: str,
    mandate_id: str | None = None,
    environment: str = "ibkr-paper",
    max_gross_notional_usd: Decimal | None = None,
) -> SupervisedMandate:
    """Grant a new mandate; an unexpired one must be revoked or expire first."""
    _require_aware(now, "now")
    if not MIN_MANDATE_TTL_S <= ttl_seconds <= MAX_MANDATE_TTL_S:
        raise SupervisedRefused(
            "mandate_ttl_out_of_bounds",
            f"ttl {ttl_seconds}s outside [{MIN_MANDATE_TTL_S}, {MAX_MANDATE_TTL_S}]",
        )
    with _locked(paths):
        if (paths.root / "HALT").exists():
            raise SupervisedRefused("effects_halted")
        if paths.mandate_revoked().exists():
            raise SupervisedRefused(
                "mandate_revoked_permanent", "remove the tombstone by hand to re-arm"
            )
        if paths.mandate().exists():
            existing = SupervisedMandate.model_validate(_read_model(paths.mandate()))
            if not existing.expired_at(now):
                raise SupervisedRefused("mandate_already_active", existing.mandate_id)
            _durable_rename(
                paths.mandate(), paths.root / f"mandate.expired-{existing.mandate_id}.json"
            )
        mandate = SupervisedMandate(
            mandate_id=mandate_id or f"mandate-{int(now.timestamp()):x}",
            environment=environment,
            max_gross_notional_usd=max_gross_notional_usd,
            account_id=account_id,
            owner_epoch=owner_epoch,
            strategy_version=strategy_version,
            profile_digest=profile_digest,
            max_orders=max_orders,
            granted_at=now,
            expires_at=shift_instant(now, ttl_seconds),
            granted_by=granted_by,
            long_running=ttl_seconds > LONG_GRANT_S,
        )
        _atomic_write(paths.mandate(), _dump(mandate))
        return mandate


def halt_effects(paths: SupervisedPaths, *, now: datetime, reason: str) -> None:
    """Persist the established HALT kill-file without waiting for effect locks.

    A remote request already in flight cannot be undone. All subsequent mandate
    grants/preflights refuse until separately reviewed recovery removes the file.
    """
    _require_aware(now, "now")
    _atomic_write(
        paths.root / "HALT",
        {"schema": "supervised-effect-halt/1", "at": now.isoformat(), "reason": reason},
    )


def revoke_mandate(
    paths: SupervisedPaths, *, now: datetime, reason: str, blocking: bool = True
) -> None:
    """Replace the mandate with a permanent tombstone."""
    _require_aware(now, "now")
    with _locked(paths, blocking=blocking):
        if not paths.mandate().exists():
            raise SupervisedRefused("mandate_absent")
        mandate = SupervisedMandate.model_validate(_read_model(paths.mandate()))
        record = _dump(mandate)
        record["revoked_at"] = now.isoformat()
        record["revoked_reason"] = reason
        _atomic_write(paths.mandate_revoked(), record)
        paths.mandate().unlink()
        _fsync_dir(paths.root)


def active_mandate(
    paths: SupervisedPaths,
    *,
    now: datetime,
    account_id: str,
    owner_epoch: str,
    strategy_version: str,
) -> SupervisedMandate:
    """Load the one mandate and refuse, with the reason, on any mismatch."""
    _require_aware(now, "now")
    if (paths.root / "HALT").exists():
        raise SupervisedRefused("effects_halted")
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


# ------------------------------------------- per-trade mandate (mandate/2)


def entry_mandate_id(mandate: SupervisedMandateV2) -> str:
    """The ``mandate_id`` a permit minted from ``mandate`` carries."""
    return ENTRY_MANDATE_ID_PREFIX + mandate.approval_id


def _entry_intent_ok(intent_id: str) -> bool:
    """Whether ``intent_id`` can name a per-trade mandate file unambiguously."""
    return bool(_ENTRY_INTENT_ID.fullmatch(intent_id)) and not intent_id.endswith(_CONSUMED_SUFFIX)


def _load_entry_mandate(path: Path) -> SupervisedMandateV2:
    from tree_options.trex.contracts.mandates import SupervisedMandateV2

    return SupervisedMandateV2.model_validate(_read_model(path))


def _permit_for_intent(paths: SupervisedPaths, intent_id: str) -> Path | None:
    for path in sorted((paths.root / "permits").glob("*.json")):
        if _read_model(path).get("intent_id") == intent_id:
            return path
    return None


def _retire_entry_mandate(
    paths: SupervisedPaths, mandate: SupervisedMandateV2, *, now: datetime, reason: str
) -> None:
    """Move an unconsumed mandate that can no longer mint out of the way."""
    source = paths.entry_mandate(mandate.intent_id)
    target = paths.entry_mandate_retired(mandate.intent_id, mandate.approval_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    _durable_rename(source, target)
    _fsync_dir(source.parent)
    _append_line(
        paths.entry_mandate_retirements(),
        {
            "schema": "supervised-mandate-retired/1",
            "intent_id": mandate.intent_id,
            "approval_id": mandate.approval_id,
            "owner_epoch": mandate.owner_epoch,
            "reason": reason,
            "at": now.isoformat(),
        },
    )


def _dead_reason(
    mandate: SupervisedMandateV2, *, now: datetime, owner_epoch: str, profile_digest: str
) -> str | None:
    """Why a stored, unconsumed mandate can never mint a permit (None = live)."""
    if mandate.expired_at(now):
        return "expired"
    if mandate.owner_epoch != owner_epoch:
        return "owner_epoch_changed"
    if mandate.profile_digest != profile_digest:
        return "profile_changed"
    return None


def grant_entry_mandate(
    paths: SupervisedPaths,
    *,
    now: datetime,
    proposal: EntryProposal,
    command: ApproveEntryCommand,
    owner_epoch: str,
    profile_digest: str,
) -> SupervisedMandateV2:
    """Grant the per-trade mandate one validated ``approve_entry`` buys.

    ``owner_epoch`` and ``profile_digest`` are the LIVE desk's bindings. The
    contract's pure ``approval_blockers`` decides first (``ApprovalRefused``
    with its codes: proposal, effect, epoch and profile mismatches,
    ``command_expired``, ``approval_expired``). Then, under the state lock:
    HALT refuses ``effects_halted``; a consumed mandate for the intent
    refuses ``mandate_already_consumed``; an intent that already holds a
    permit refuses ``permit_already_issued``; a replay of the SAME approval
    returns the stored mandate unchanged (one approval, one order); another
    approval refuses ``entry_mandate_already_granted`` while the stored one
    can still mint, and retires it once it cannot (expired, earlier owner
    epoch, other profile). Nothing here contacts a broker.
    """
    from tree_options.trex.contracts.mandates import approval_blockers, mandate_for_approval
    from tree_options.trex.contracts.vocabulary import Blocker

    _require_aware(now, "now")
    blockers = approval_blockers(
        proposal, command, now=now, owner_epoch=owner_epoch, profile_digest=profile_digest
    )
    if blockers:
        raise ApprovalRefused(blockers)
    intent_id = proposal.effect.intent_id
    if not _entry_intent_ok(intent_id):
        raise SupervisedRefused("intent_id_not_path_safe", intent_id)
    with _locked(paths):
        if (paths.root / "HALT").exists():
            raise SupervisedRefused("effects_halted")
        if paths.entry_mandate_consumed(intent_id).exists():
            raise ApprovalRefused((Blocker.MANDATE_ALREADY_CONSUMED,), intent_id)
        issued = _permit_for_intent(paths, intent_id)
        if issued is not None:
            raise SupervisedRefused("permit_already_issued", issued.name)
        current = paths.entry_mandate(intent_id)
        if current.exists():
            existing = _load_entry_mandate(current)
            if existing.approval_id == command.idempotency_key:
                if existing.proposal_id != proposal.proposal_id:
                    raise ApprovalRefused((Blocker.COMMAND_BODY_MISMATCH,), intent_id)
                return existing
            dead = _dead_reason(
                existing, now=now, owner_epoch=owner_epoch, profile_digest=profile_digest
            )
            if dead is None:
                raise SupervisedRefused("entry_mandate_already_granted", existing.approval_id)
            _retire_entry_mandate(paths, existing, now=now, reason=dead)
        mandate = mandate_for_approval(proposal, command, now=now)
        _atomic_write(current, _dump(mandate))
        return mandate


def invalidate_entry_mandates(
    paths: SupervisedPaths, *, now: datetime, owner_epoch: str
) -> list[str]:
    """Retire every unconsumed per-trade mandate of another owner epoch.

    A desk restart mints a new epoch, so its pending approvals can never
    mint (``approval_epoch_mismatch``); this moves them out of the way and
    returns their intent ids, for the ``approvals_invalidated`` event.
    Consumed mandates stay as history; an unreadable file is left in place
    (it cannot mint either: loading it refuses).
    """
    _require_aware(now, "now")
    retired: list[str] = []
    with _locked(paths):
        directory = paths.entry_mandates()
        if not directory.is_dir():
            return retired
        for path in sorted(directory.glob("*.json")):
            if path.name.endswith(_CONSUMED_SUFFIX + ".json"):
                continue
            try:
                mandate = _load_entry_mandate(path)
            except (OSError, ValueError):
                continue
            if mandate.owner_epoch != owner_epoch:
                _retire_entry_mandate(paths, mandate, now=now, reason="owner_epoch_changed")
                retired.append(mandate.intent_id)
    return retired


def entry_mandates(paths: SupervisedPaths, *, now: datetime) -> list[dict[str, Any]]:
    """Read-only inventory of per-trade mandates (never raises on a bad file).

    ``state``: ``granted`` (may still mint), ``expired``, ``consumed`` (its
    permit was minted) or ``unreadable``.
    """
    _require_aware(now, "now")
    directory = paths.entry_mandates()
    if not directory.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        consumed = path.name.endswith(_CONSUMED_SUFFIX + ".json")
        stem = path.name.removesuffix(".json").removesuffix(_CONSUMED_SUFFIX if consumed else "")
        row: dict[str, Any] = {"intent_id": stem, "file": path.name}
        try:
            mandate = _load_entry_mandate(path)
        except (OSError, ValueError) as error:
            row.update(state="unreadable", error=type(error).__name__)
        else:
            if consumed:
                state = "consumed"
            else:
                state = "expired" if mandate.expired_at(now) else "granted"
            row.update(
                state=state,
                approval_id=mandate.approval_id,
                proposal_id=mandate.proposal_id,
                effect_sha256=mandate.effect_sha256,
                owner_epoch=mandate.owner_epoch,
                strategy_version=mandate.strategy_version,
                expires_at=mandate.expires_at.isoformat(),
            )
        rows.append(row)
    return rows


def _equity_effect_payload(effect_payload: bytes) -> bool:
    """True for a ``supervised-equity-effect/1`` payload, which must then be
    its canonical encoding (else ``effect_invalid``). Any other payload stays
    opaque bytes, hashed as before."""
    from tree_options.trex.contracts.effects import EQUITY_EFFECT_SCHEMA, decode_effect_v1

    try:
        document = json.loads(effect_payload)
    except ValueError:
        return False
    if not isinstance(document, dict) or document.get("schema") != EQUITY_EFFECT_SCHEMA:
        return False
    try:
        decode_effect_v1(effect_payload)
    except ValueError as error:
        raise SupervisedRefused("effect_invalid", type(error).__name__) from None
    return True


def _entry_mandate_for_permit(
    paths: SupervisedPaths, permit: EffectPermit
) -> SupervisedMandateV2 | None:
    """The consumed per-trade mandate behind ``permit``; None for a v1 permit.

    A permit is per-trade when its ``mandate_id`` says so OR its intent has a
    consumed mandate/2; either way the two must agree, else it refuses."""
    intent_id = permit.intent_id
    consumed = paths.entry_mandate_consumed(intent_id) if _entry_intent_ok(intent_id) else None
    per_trade = permit.mandate_id.startswith(ENTRY_MANDATE_ID_PREFIX)
    if not per_trade and (consumed is None or not consumed.exists()):
        return None
    if (paths.root / "HALT").exists():
        raise SupervisedRefused("effects_halted")
    if consumed is None or not consumed.exists():
        raise SupervisedRefused("mandate_absent", intent_id)
    mandate = _load_entry_mandate(consumed)
    if (
        entry_mandate_id(mandate) != permit.mandate_id
        or mandate.intent_id != intent_id
        or mandate.effect_sha256 != permit.effect_sha256
        or mandate.account_id != permit.account_id
        or mandate.owner_epoch != permit.owner_epoch
        or mandate.strategy_version != permit.strategy_version
    ):
        raise SupervisedRefused("mandate_binding_mismatch", permit.permit_id)
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
        for used in (
            paths.sending(intent_id),
            paths.terminal(intent_id),
            paths.reconciled(intent_id),
        ):
            if used.exists():
                raise SupervisedRefused("intent_id_reused", used.name)
        # the new intent's creation time is the reference "now"
        blocked = _in_flight_reason(paths, intent.package_intent_sha256, intent.created_at)
        if blocked is not None:
            raise SupervisedRefused(blocked[0], blocked[1])
        _atomic_write(path, _dump(intent))
        return path


_TERMINAL_STATES = {
    "acknowledged": _OutboxState.RECEIPT,
    "rejected": _OutboxState.REJECTED,
    "uncertain": _OutboxState.UNCERTAIN,
}


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


def _in_flight_reason(
    paths: SupervisedPaths, package_sha: str, now: datetime
) -> tuple[str, str] | None:
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
        if state in (
            _OutboxState.PENDING,
            _OutboxState.SENDING,
            _OutboxState.RECEIPT,
            _OutboxState.UNCERTAIN,
        ):
            return ("package_already_in_flight", f"{intent_id}:{state.value}")
    return None


# ---------------------------------------------------------------- permit


def issue_permit(
    paths: SupervisedPaths,
    *,
    now: datetime,
    account_id: str,
    owner_epoch: str,
    intent: SupervisedIntent,
    canary_blockers: Sequence[str],
    effect_payload: bytes,
    screening_sha256: str,
    ttl_seconds: int = MAX_PERMIT_TTL_S,
    profile_digest: str | None = None,
) -> EffectPermit:
    """Spend one mandate order on a hash-bound permit, or refuse.

    Refusal reasons: canary blockers, a passed send deadline, an inactive
    or exhausted mandate, or a permit that already exists for the intent.
    The permit expires at the earlier of its TTL and the send deadline.

    An intent with a per-trade mandate (``mandates/<intent_id>.json``) is
    authorized by it alone (``_issue_entry_permit``), checked against the
    LIVE ``owner_epoch`` and ``profile_digest`` and consumed exactly once;
    an equity effect without one refuses ``mandate_absent``. Every other
    intent takes the v1 path below, unchanged.
    """
    _require_aware(now, "now")
    if canary_blockers:
        raise SupervisedRefused("canary_blockers", ",".join(canary_blockers))
    if _reached(now, intent.send_deadline):
        raise SupervisedRefused("intent_deadline_passed")
    if not 1 <= ttl_seconds <= MAX_PERMIT_TTL_S:
        raise SupervisedRefused("permit_ttl_out_of_bounds")
    equity = _equity_effect_payload(effect_payload)
    with _locked(paths):
        intent_id = intent.intent.intent_id
        if _entry_intent_ok(intent_id) and (
            paths.entry_mandate(intent_id).exists()
            or paths.entry_mandate_consumed(intent_id).exists()
        ):
            return _issue_entry_permit(
                paths,
                now=now,
                account_id=account_id,
                owner_epoch=owner_epoch,
                profile_digest=profile_digest,
                intent=intent,
                effect_payload=effect_payload,
                screening_sha256=screening_sha256,
                ttl_seconds=ttl_seconds,
            )
        if equity:
            raise SupervisedRefused(
                "mandate_absent", f"{intent_id}: an equity entry needs a per-trade approval"
            )
        # The intent's emitting source must equal the granted strategy
        # scope; a mismatch refuses here as mandate_scope_mismatch.
        mandate = active_mandate(
            paths,
            now=now,
            account_id=account_id,
            owner_epoch=owner_epoch,
            strategy_version=intent.intent.source,
        )
        if mandate.orders_used >= mandate.max_orders:
            raise SupervisedRefused("mandate_budget_exhausted")
        for path in sorted((paths.root / "permits").glob("*.json")):
            if _read_model(path).get("intent_id") == intent.intent.intent_id:
                raise SupervisedRefused("permit_already_issued", path.name)
        effect_sha = hashlib.sha256(effect_payload).hexdigest()
        permit_id = (
            "permit-"
            + hashlib.sha256(
                canonical_bytes({"intent_id": intent.intent.intent_id, "effect_sha256": effect_sha})
            ).hexdigest()[:16]
        )
        expires_at = min(shift_instant(now, ttl_seconds), intent.send_deadline)
        permit = EffectPermit(
            permit_id=permit_id,
            mandate_id=mandate.mandate_id,
            intent_id=intent.intent.intent_id,
            package_intent_sha256=intent.package_intent_sha256,
            effect_sha256=effect_sha,
            account_id=account_id,
            owner_epoch=owner_epoch,
            strategy_version=mandate.strategy_version,
            screening_sha256=screening_sha256,
            issued_at=now,
            expires_at=expires_at,
        )
        # Budget first: a crash between the writes wastes an order, never
        # mints one.
        spent = mandate.model_copy(update={"orders_used": mandate.orders_used + 1})
        _atomic_write(paths.mandate(), _dump(spent))
        _atomic_write(paths.permit(permit_id), _dump(permit))
        return permit


def _issue_entry_permit(
    paths: SupervisedPaths,
    *,
    now: datetime,
    account_id: str,
    owner_epoch: str,
    profile_digest: str | None,
    intent: SupervisedIntent,
    effect_payload: bytes,
    screening_sha256: str,
    ttl_seconds: int,
) -> EffectPermit:
    """Mint the one permit a per-trade mandate allows (caller holds the lock).

    ``mandate_blockers`` (approval expired, epoch, profile, effect hash)
    refuses as ``ApprovalRefused`` without consuming; a consumed mandate
    refuses ``mandate_already_consumed``. The permit expires at the
    earliest of its TTL, the intent's send deadline and the approval.
    """
    from tree_options.trex.contracts.mandates import mandate_blockers
    from tree_options.trex.contracts.vocabulary import Blocker

    intent_id = intent.intent.intent_id
    if (paths.root / "HALT").exists():
        raise SupervisedRefused("effects_halted")
    pending = paths.entry_mandate(intent_id)
    consumed = paths.entry_mandate_consumed(intent_id)
    if consumed.exists():
        raise ApprovalRefused((Blocker.MANDATE_ALREADY_CONSUMED,), intent_id)
    mandate = _load_entry_mandate(pending)
    if mandate.intent_id != intent_id:
        raise SupervisedRefused("mandate_binding_mismatch", pending.name)
    effect_sha = hashlib.sha256(effect_payload).hexdigest()
    blockers = mandate_blockers(
        mandate,
        now=now,
        effect_sha256=effect_sha,
        owner_epoch=owner_epoch,
        # an unknown live profile can never match the approved one
        profile_digest=profile_digest or "",
    )
    if blockers:
        raise ApprovalRefused(blockers, intent_id)
    if mandate.account_id != account_id:
        raise SupervisedRefused("mandate_account_mismatch")
    if mandate.strategy_version != intent.intent.source:
        raise SupervisedRefused("mandate_scope_mismatch")
    issued = _permit_for_intent(paths, intent_id)
    if issued is not None:
        raise SupervisedRefused("permit_already_issued", issued.name)
    permit_id = (
        "permit-"
        + hashlib.sha256(
            canonical_bytes({"intent_id": intent_id, "effect_sha256": effect_sha})
        ).hexdigest()[:16]
    )
    permit = EffectPermit(
        permit_id=permit_id,
        mandate_id=entry_mandate_id(mandate),
        intent_id=intent_id,
        package_intent_sha256=intent.package_intent_sha256,
        effect_sha256=effect_sha,
        account_id=account_id,
        owner_epoch=owner_epoch,
        strategy_version=mandate.strategy_version,
        screening_sha256=screening_sha256,
        issued_at=now,
        expires_at=min(shift_instant(now, ttl_seconds), intent.send_deadline, mandate.expires_at),
    )
    # Consumption first (the rename IS the consumption): a crash between
    # the two steps wastes the approval, never mints a second order.
    _durable_rename(pending, consumed)
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


def _fold(lifecycle: ExecutionLifecycle, records: Sequence[ExecutionRecord]) -> ExecutionLifecycle:
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


def _uncertain_receipt(
    permit: EffectPermit, now: datetime, reason: str, detail: str
) -> dict[str, Any]:
    return {
        "schema": "supervised-send-receipt/1",
        "intent_id": permit.intent_id,
        "outcome": "uncertain",
        "reason": reason,
        "detail": detail,
        "package_intent_sha256": permit.package_intent_sha256,
        "permit_id": permit.permit_id,
        "at": now.isoformat(),
    }


def send(
    paths: SupervisedPaths,
    *,
    now: datetime,
    permit_id: str,
    effect_payload: bytes,
    broker: SupervisedBroker,
) -> dict[str, Any]:
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
        per_trade = _entry_mandate_for_permit(paths, permit)
        environment: str
        if per_trade is not None:
            environment = per_trade.environment
        else:
            environment = active_mandate(
                paths,
                now=now,
                account_id=permit.account_id,
                owner_epoch=permit.owner_epoch,
                strategy_version=permit.strategy_version,
            ).environment
        broker_environment = getattr(broker, "execution_environment", "ibkr-paper")
        if environment != broker_environment:
            raise SupervisedRefused("broker_environment_mismatch")
        pending = paths.pending(permit.intent_id)
        if not pending.exists():
            if (
                paths.terminal(permit.intent_id).exists()
                or paths.reconciled(permit.intent_id).exists()
            ):
                raise SupervisedRefused("intent_terminal", permit.intent_id)
            if paths.sending(permit.intent_id).exists():
                raise SupervisedRefused("intent_in_flight", permit.intent_id)
            raise SupervisedRefused("intent_absent", permit.intent_id)
        intent = SupervisedIntent.model_validate(_read_model(pending))
        if intent.package_intent_sha256 != permit.package_intent_sha256:
            raise SupervisedRefused("intent_permit_binding_mismatch", permit.intent_id)
        if _reached(now, intent.send_deadline):
            raise SupervisedRefused("intent_deadline_passed", permit.intent_id)
        preflight = getattr(broker, "validate_effect", None)
        if environment == "broker_paper" and not callable(preflight):
            raise SupervisedRefused("broker_paper_preflight_absent")
        if callable(preflight):
            preflight(effect_payload, intent.intent, permit)
        # Durable claim before any broker effect: the rename IS the claim.
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
            record_id=f"sup-send-{permit_id}",
            intent_id=permit.intent_id,
            send_attempt_at=now,
            source="supervised",
            source_sequence_id=permit_id,
        )
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
            receipt = {
                "schema": "supervised-send-receipt/1",
                "intent_id": permit.intent_id,
                "outcome": "acknowledged",
                "broker_order_id": outcome.acknowledgement.broker_order_id,
                "package_intent_sha256": permit.package_intent_sha256,
                "permit_id": permit_id,
                "at": now.isoformat(),
            }
        elif isinstance(outcome, Refused):
            receipt = {
                "schema": "supervised-send-receipt/1",
                "intent_id": permit.intent_id,
                "outcome": "rejected",
                "reason_code": outcome.reject.reason_code,
                "package_intent_sha256": permit.package_intent_sha256,
                "permit_id": permit_id,
                "at": now.isoformat(),
            }
        else:
            if outcome.observation == "timeout":
                uncertainty: ExecutionRecord = TimeoutObserved(
                    record_id=f"sup-timeout-{permit_id}",
                    intent_id=permit.intent_id,
                    attempt_id=attempt.record_id,
                    locally_received_at=now,
                    source="supervised",
                    source_sequence_id=f"timeout-{permit_id}",
                )
            elif outcome.observation == "disconnect":
                uncertainty = DisconnectObserved(
                    record_id=f"sup-disconnect-{permit_id}",
                    intent_id=permit.intent_id,
                    locally_received_at=now,
                    source="supervised",
                    source_sequence_id=f"disconnect-{permit_id}",
                )
            else:
                uncertainty = UncertaintyObserved(
                    record_id=f"sup-uncertain-{permit_id}",
                    intent_id=permit.intent_id,
                    locally_received_at=now,
                    reason_code=outcome.reason,
                    source="supervised",
                    source_sequence_id=f"uncertain-{permit_id}",
                )
            _journal_append(paths, uncertainty)
            receipt = _uncertain_receipt(permit, now, outcome.reason, outcome.detail)
        _atomic_write(paths.terminal(permit.intent_id), receipt)
        paths.sending(permit.intent_id).unlink(missing_ok=True)
        _fsync_dir(paths.outbox_dir())
        return receipt


# ------------------------------------------------------------ reconcile


def reconcile_intent(
    paths: SupervisedPaths, *, now: datetime, intent_id: str, broker: SupervisedBroker
) -> dict[str, Any]:
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
        settle_at = shift_instant(datetime.fromisoformat(str(terminal["at"])), RECONCILE_SETTLE_S)
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
        record = {
            "schema": "supervised-reconciliation/1",
            "intent_id": intent_id,
            "verdict": outcome,
            "detail": detail,
            "at": now.isoformat(),
            "package_intent_sha256": terminal.get("package_intent_sha256"),
            "broker_order_id": getattr(verdict, "broker_order_id", None),
        }
        if outcome == "still_uncertain":
            _append_line(paths.reconcile_audit(intent_id), record)
            return {**record, "persisted": False}
        if prior is not None:
            if prior.get("verdict") != outcome:
                raise SupervisedRefused(
                    "reconcile_verdict_conflict", f"{prior.get('verdict')} -> {outcome}"
                )
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
            claim = _read_model(path)
            package_sha = claim.get("package_intent_sha256")
            if (
                not paths.journal(intent_id).exists()
                and claim.get("claimed_at")
                and claim.get("permit_id")
            ):
                order = OrderIntent.model_validate(claim["intent"])
                _journal_append(paths, order)
                _journal_append(
                    paths,
                    SubmitAttempt(
                        record_id=f"sup-send-{claim['permit_id']}",
                        intent_id=intent_id,
                        send_attempt_at=datetime.fromisoformat(claim["claimed_at"]),
                        source="supervised-recovered-claim",
                        source_sequence_id=claim["permit_id"],
                    ),
                )
                _journal_append(
                    paths,
                    UncertaintyObserved(
                        record_id=f"sup-recovery-{claim['permit_id']}",
                        intent_id=intent_id,
                        locally_received_at=now,
                        reason_code="process_restart_after_durable_claim",
                        source="supervised-recovered-claim",
                        source_sequence_id=f"recovery-{claim['permit_id']}",
                    ),
                )
        except (OSError, ValueError):
            package_sha = None
        receipt = {
            "schema": "supervised-send-receipt/1",
            "intent_id": intent_id,
            "outcome": "uncertain",
            "reason": "sending_orphan_recovered",
            "detail": "process died between claim and terminal write",
            "package_intent_sha256": package_sha,
            "at": now.isoformat(),
        }
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
        report["mandate"] = {
            "state": "expired" if mandate.expired_at(now) else "active",
            "days_left": mandate.days_left(now),
            **_dump(mandate),
        }
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
        description="Operator surface for the supervised paper path (no broker contact).",
    )
    parser.add_argument(
        "--dir", type=Path, default=None, help="state directory (default TREX_SUPERVISED_DIR)"
    )
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
    grant.add_argument(
        "--environment", choices=("ibkr-paper", "broker_paper"), default="ibkr-paper"
    )
    grant.add_argument("--max-gross-notional-usd", type=Decimal, default=None)
    sub.add_parser("revoke", help="revoke the active mandate").add_argument("--reason", default="")
    sub.add_parser("status", help="read-only inventory")
    sub.add_parser("recover", help="reclassify orphan sending claims")
    args = parser.parse_args()
    paths = SupervisedPaths(args.dir.expanduser() if args.dir else SupervisedPaths.default().root)
    try:
        if args.command == "grant":
            mandate = grant_mandate(
                paths,
                now=now,
                account_id=args.account,
                owner_epoch=args.owner_epoch,
                strategy_version=args.strategy,
                profile_digest=args.profile_digest,
                max_orders=args.max_orders,
                ttl_seconds=args.ttl_seconds,
                granted_by=args.granted_by,
                environment=args.environment,
                max_gross_notional_usd=args.max_gross_notional_usd,
            )
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
