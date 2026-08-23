"""The run-state facade: identity, transitions, status, resume validation.

Store layout (durable, gitignored, NEVER `/tmp` — constraint 9):

    artifacts/runstate/<run-id>/
      run.json         # immutable RunIdentity, written once at PLANNED
      journal.jsonl    # append-only hash-chained authority (journal.py)
      current.json     # atomic projection of the journal
      lease/owner.json # exclusive-launch lease (lease.py)
      heartbeat.json   # liveness beat (heartbeat.py)

The run-id is DETERMINISTIC in the run's inputs (campaign, protocol hash,
code sha, universe manifest hash, args hash, start date), so a resume after
reboot recomputes the same id and finds the same store — no orphan stores,
no double stores for one logical run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from tree_options.data.digest import sha256_hex
from tree_options.runstate import heartbeat as hb_module
from tree_options.runstate import journal as journal_module
from tree_options.runstate import lease as lease_module
from tree_options.runstate.errors import (
    IllegalTransitionError,
    ManifestMismatchError,
    StoreExistsError,
    UnknownRunError,
)
from tree_options.runstate.lease import LeaseClassification, LeaseOwner
from tree_options.runstate.states import (
    LEGAL_EDGES,
    RunIdentity,
    RunState,
    is_legal,
)

RUN_FILENAME = "run.json"
DEFAULT_STORE_ROOT = Path("artifacts/runstate")


def compute_run_id(
    *,
    campaign: str,
    protocol_hash: str,
    code_sha: str,
    universe_manifest_sha256: str,
    args_hash: str,
    started_epoch: int,
) -> str:
    """Deterministic run id: same inputs -> same store on resume."""
    digest = sha256_hex(
        json.dumps(
            {
                "campaign": campaign,
                "protocol_hash": protocol_hash,
                "code_sha": code_sha,
                "universe_manifest_sha256": universe_manifest_sha256,
                "args_hash": args_hash,
                "start_date": _utc_date(started_epoch),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return f"{campaign}-{_utc_date(started_epoch).replace('-', '')}-{digest[:8]}"


def _utc_date(epoch: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, tz=UTC).date().isoformat()


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    state: RunState | None
    heartbeat_class: hb_module.HeartbeatClass
    lease_class: LeaseClassification | None
    tail_hash: str
    seq: int
    tail_damaged: bool
    pinned_manifest_sha256: str | None
    failure_reason: str | None


class RunStore:
    """One run's durable state. Open is read-safe; writes go through
    `transition`/`pin_manifest` (lease-holding callers only)."""

    def __init__(self, store_dir: Path, identity: RunIdentity) -> None:
        self.dir = store_dir
        self.identity = identity
        self._view = journal_module.replay(store_dir, run_id=identity.run_id)

    # -- construction ---------------------------------------------------

    @classmethod
    def create(cls, root: Path, identity: RunIdentity, *, now_epoch: int) -> RunStore:
        store_dir = root / identity.run_id
        run_path = store_dir / RUN_FILENAME
        if store_dir.exists():
            raise StoreExistsError(identity.run_id)
        store_dir.mkdir(parents=True)
        run_path.write_text(
            json.dumps(json.loads(identity.model_dump_json()), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        store = cls(store_dir, identity)
        genesis = journal_module.JournalRecord(
            seq=1,
            kind="GENESIS",
            to_state=RunState.PLANNED,
            reason="store created",
            actor_pid=identity.pid,
            actor_boot_id=identity.boot_id,
            at_epoch=now_epoch,
            prev_record_sha256=journal_module.GENESIS_PREV,
        )
        journal_module.append_record(store_dir, genesis)
        store._rewrite_projection(now_epoch)
        return store

    @classmethod
    def open(cls, root: Path, run_id: str) -> RunStore:
        store_dir = root / run_id
        run_path = store_dir / RUN_FILENAME
        if not run_path.exists():
            raise UnknownRunError(run_id)
        identity = RunIdentity.model_validate(json.loads(run_path.read_text(encoding="utf-8")))
        return cls(store_dir, identity)

    # -- queries ----------------------------------------------------------

    @property
    def state(self) -> RunState | None:
        for record in reversed(self._view.records):
            if record.to_state is not None:
                return record.to_state
        return None

    @property
    def pinned_manifest_sha256(self) -> str | None:
        for record in reversed(self._view.records):
            if record.kind == "MANIFEST_PINNED":
                return record.manifest_sha256
        return self.identity.capture_manifest_sha256

    def refresh(self) -> None:
        """Re-read the journal (after another process appended)."""
        self._view = journal_module.replay(self.dir, run_id=self.identity.run_id)

    def status(
        self,
        *,
        now_epoch: int,
        boot_id_now: str,
        proc_root: Path | None = None,
    ) -> RunStatus:
        state = self.state
        beat = hb_module.read(self.dir)
        hb_class = hb_module.classify(
            beat,
            state,
            now_epoch=now_epoch,
            boot_id_now=boot_id_now,
            proc_root=proc_root,
        )
        lease_class: LeaseClassification | None = None
        if (self.dir / lease_module.LEASE_DIRNAME / lease_module.OWNER_FILENAME).exists():
            lease_class = lease_module.classify_existing(
                self.dir, boot_id_now=boot_id_now, proc_root=proc_root
            )
        failure_reason = None
        if state is RunState.FAILED:
            for record in reversed(self._view.records):
                if record.kind == "TRANSITION" and record.to_state is RunState.FAILED:
                    failure_reason = record.reason
                    break
        return RunStatus(
            run_id=self.identity.run_id,
            state=state,
            heartbeat_class=hb_class,
            lease_class=lease_class,
            tail_hash=self._view.tail_hash,
            seq=self._view.records[-1].seq if self._view.records else 0,
            tail_damaged=self._view.tail_damaged,
            pinned_manifest_sha256=self.pinned_manifest_sha256,
            failure_reason=failure_reason,
        )

    # -- writes -------------------------------------------------------------

    def transition(
        self,
        to_state: RunState,
        *,
        reason: str,
        now_epoch: int,
        actor_pid: int,
        actor_boot_id: str,
        owner: LeaseOwner | None = None,
    ) -> str:
        """Journal one legal transition + rewrite the projection. Fails
        closed on skips, regressions, and UNKNOWN targets."""
        source = self.state
        if source is None:
            raise IllegalTransitionError(
                self.identity.run_id, "journal has no state yet (GENESIS missing)"
            )
        if owner is not None and owner.boot_id != actor_boot_id:
            # A lease from a previous boot cannot authorize a transition on
            # this boot.
            raise IllegalTransitionError(
                self.identity.run_id,
                f"lease boot {owner.boot_id[:8]}… != actor boot {actor_boot_id[:8]}…",
            )
        if not is_legal(source, to_state):
            if source is RunState.UNKNOWN or to_state is RunState.UNKNOWN:
                detail = "UNKNOWN is a classification, never a journal target"
            elif to_state not in LEGAL_EDGES.get(source, frozenset()):
                detail = f"{source.value} -> {to_state.value} is not a legal edge"
            else:
                detail = f"{source.value} -> {to_state.value} skips or regresses"
            raise IllegalTransitionError(self.identity.run_id, detail)
        record = journal_module.JournalRecord(
            seq=self._view.records[-1].seq + 1 if self._view.records else 1,
            kind="TRANSITION",
            from_state=source,
            to_state=to_state,
            reason=reason,
            actor_pid=actor_pid,
            actor_boot_id=actor_boot_id,
            at_epoch=now_epoch,
            prev_record_sha256=self._view.tail_hash,
        )
        digest = journal_module.append_record(self.dir, record)
        self.refresh()
        self._rewrite_projection(now_epoch)
        return digest

    def pin_manifest(
        self, manifest_sha256: str, *, now_epoch: int, actor_pid: int, actor_boot_id: str
    ) -> str:
        """Record the capture manifest hash the run's evidence is bound to."""
        record = journal_module.JournalRecord(
            seq=self._view.records[-1].seq + 1 if self._view.records else 1,
            kind="MANIFEST_PINNED",
            reason="capture manifest pinned",
            actor_pid=actor_pid,
            actor_boot_id=actor_boot_id,
            at_epoch=now_epoch,
            manifest_sha256=manifest_sha256,
            prev_record_sha256=self._view.tail_hash,
        )
        digest = journal_module.append_record(self.dir, record)
        self.refresh()
        self._rewrite_projection(now_epoch)
        return digest

    def validate_resume(self, observed_manifest_sha256: str) -> None:
        """Refuse to resume against a manifest the journal never pinned.

        A mismatch is an incident, not an input: the manifest is derived
        evidence and hand-repair is prohibited (runbook §manifest-repair).
        """
        pinned = self.pinned_manifest_sha256
        if pinned is None:
            raise ManifestMismatchError(
                self.identity.run_id, "<never-pinned>", observed_manifest_sha256
            )
        if pinned != observed_manifest_sha256:
            raise ManifestMismatchError(self.identity.run_id, pinned, observed_manifest_sha256)

    def write_heartbeat(self, beat: hb_module.Heartbeat) -> None:
        hb_module.write(self.dir, beat)

    def _rewrite_projection(self, now_epoch: int) -> None:
        self.refresh()
        projection = journal_module.build_projection(
            self.identity.run_id, self._view, written_at_epoch=now_epoch
        )
        journal_module.write_projection(self.dir, projection)

    def rebuild_projection(self, *, now_epoch: int) -> None:
        """Writer-only repair of a torn projection from the journal."""
        self._rewrite_projection(now_epoch)
