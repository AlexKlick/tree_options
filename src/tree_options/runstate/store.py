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
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from tree_options.data.digest import sha256_hex
from tree_options.runstate import custody
from tree_options.runstate import heartbeat as hb_module
from tree_options.runstate import journal as journal_module
from tree_options.runstate import lease as lease_module
from tree_options.runstate.errors import (
    IllegalTransitionError,
    ManifestMismatchError,
    NonCanonicalRunIdError,
    PinAlreadyBoundError,
    RunIdRefusedError,
    StoreCustodyError,
    StoreExistsError,
    StoreIdMismatchError,
    StoreRootRefusedError,
    UnknownRunError,
)
from tree_options.runstate.lease import LeaseClassification, LeaseOwner
from tree_options.runstate.states import (
    LEGAL_EDGES,
    RunIdentity,
    RunIdentityCore,
    RunState,
    is_legal,
)

RUN_FILENAME = "run.json"
DEFAULT_STORE_ROOT = Path("artifacts/runstate")


def _validate_store_root(root: Path) -> None:
    """Refuse any lexical root under /tmp before filesystem mutation.

    /tmp is wiped on reboot — a run-state store there is a lie. The
    journal, lease, and pinned manifest are authoritative evidence;
    they must live under a durable root (default: artifacts/runstate).
    Component-boundary check matches seal/ledger semantics.
    """
    absolute = custody.lexical_absolute(root)
    tmp = Path("/tmp")
    if absolute == tmp or tmp in absolute.parents:
        raise StoreRootRefusedError(str(root))


def _validated_store_dir(root: Path, run_id: str) -> Path:
    """The run's store directory, refusing any run id that escapes the root.

    Round-2 review fix (2026-08-23, probe: `root / "/tmp/pr-a-escaped"`
    landed the store OUTSIDE the validated root): pathlib joining an
    ABSOLUTE run id replaces the base, and parent-bearing ids escape
    upward, so `root / identity.run_id` validated only the root. The store
    dir is now derived through this helper, which enforces that the run id
    is exactly one path component AND that the FINAL resolved directory is
    a descendant of the resolved root (a symlinked run-id directory
    pointing outside the root is refused too).
    """
    _validate_store_root(root)
    if (
        run_id == ""
        or run_id == "."
        or run_id == ".."
        or Path(run_id).is_absolute()
        or Path(run_id).name != run_id
    ):
        raise RunIdRefusedError(str(root), run_id)
    # Deliberately do not resolve here.  Component-wise custody must see and
    # refuse every pre-existing symlink rather than erase it first.
    return root / run_id


def compute_run_id(
    *,
    campaign: str,
    protocol_hash: str,
    code_sha: str,
    provider: str,
    capture_version: str,
    universe_manifest_sha256: str,
    args_hash: str,
    started_epoch: int,
    run_nonce: str | None = None,
) -> str:
    """Compute the sole valid id for one canonical logical run core."""
    core = RunIdentityCore(
        campaign=campaign,
        protocol_hash=protocol_hash,
        code_sha=code_sha,
        provider=provider,
        capture_version=capture_version,
        universe_manifest_sha256=universe_manifest_sha256,
        args_hash=args_hash,
        logical_start_date=_utc_date(started_epoch),
        run_nonce=run_nonce,
    )
    digest = sha256_hex(
        json.dumps(
            core.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return f"{campaign}-{_utc_date(started_epoch).strftime('%Y%m%d')}-{digest[:8]}"


def canonical_run_id(identity: RunIdentity) -> str:
    """Recompute an identity's run id without process-incarnation fields."""
    return compute_run_id(
        campaign=identity.campaign,
        protocol_hash=identity.protocol_hash,
        code_sha=identity.code_sha,
        provider=identity.provider,
        capture_version=identity.capture_version,
        universe_manifest_sha256=identity.universe_manifest_sha256,
        args_hash=identity.args_hash,
        started_epoch=identity.started_epoch,
        run_nonce=identity.run_nonce,
    )


def _validate_canonical_run_id(root: Path, identity: RunIdentity) -> None:
    expected = canonical_run_id(identity)
    if identity.run_id != expected:
        raise NonCanonicalRunIdError(str(root), identity.run_id, expected)


def _utc_date(epoch: int) -> date:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, tz=UTC).date()


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

    def __init__(
        self,
        store_dir: Path,
        identity: RunIdentity,
        *,
        _view: journal_module.JournalView | None = None,
    ) -> None:
        self.dir = store_dir
        self.identity = identity
        self._view = _view or journal_module.replay(store_dir, run_id=identity.run_id)

    # -- construction ---------------------------------------------------

    @classmethod
    def create(cls, root: Path, identity: RunIdentity, *, now_epoch: int) -> RunStore:
        # Round-2 review fix: derive the store dir through the validated
        # helper — `root / identity.run_id` alone let an absolute or
        # parent-bearing run id REPLACE the validated root.
        store_dir = _validated_store_dir(root, identity.run_id)
        # External PR #13 audit: reject an operator-chosen id after the
        # read-only path-shape/root checks but before any filesystem mutation.
        # One logical core has one durable store, including across reboots.
        _validate_canonical_run_id(root, identity)
        root_fd = custody.open_directory(
            root,
            create=True,
            run_id=identity.run_id,
            purpose="run-state root",
        )
        assert root_fd is not None
        store_fd: int | None = None
        try:
            try:
                os.mkdir(identity.run_id, 0o755, dir_fd=root_fd)
            except FileExistsError:
                existing_fd = custody.open_child_directory(
                    root_fd,
                    identity.run_id,
                    create=False,
                    run_id=identity.run_id,
                    purpose="run store",
                )
                if existing_fd is not None:
                    os.close(existing_fd)
                raise StoreExistsError(identity.run_id) from None
            store_fd = custody.open_child_directory(
                root_fd,
                identity.run_id,
                create=False,
                run_id=identity.run_id,
                purpose="run store",
            )
            assert store_fd is not None
            os.fsync(root_fd)
            identity_bytes = (
                json.dumps(json.loads(identity.model_dump_json()), indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            custody.atomic_write(
                store_dir,
                store_fd,
                RUN_FILENAME,
                identity_bytes,
                run_id=identity.run_id,
                purpose="immutable run.json identity",
                mode=0o644,
                exclusive=True,
            )
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
            journal_module.append_record(store_dir, genesis, _dir_fd=store_fd)
            view = journal_module.replay(
                store_dir,
                run_id=identity.run_id,
                _dir_fd=store_fd,
            )
            projection = journal_module.build_projection(
                identity.run_id,
                view,
                written_at_epoch=now_epoch,
            )
            journal_module.write_projection(store_dir, projection, _dir_fd=store_fd)
            custody.verify_directory_identity(store_dir, store_fd, run_id=identity.run_id)
            return cls(store_dir, identity, _view=view)
        finally:
            if store_fd is not None:
                os.close(store_fd)
            os.close(root_fd)

    @classmethod
    def open(cls, root: Path, run_id: str) -> RunStore:
        # Round-2 review fix: same derivation-time refusal as create().
        store_dir = _validated_store_dir(root, run_id)
        store_fd = custody.open_directory(
            store_dir,
            create=False,
            run_id=run_id,
            purpose="run-state store",
        )
        if store_fd is None:
            raise UnknownRunError(run_id)
        try:
            raw = custody.read_named_bytes(
                store_dir,
                store_fd,
                RUN_FILENAME,
                run_id=run_id,
                purpose="immutable run.json identity",
                allow_missing=True,
            )
            if raw is None:
                raise UnknownRunError(run_id)
            identity = RunIdentity.model_validate(json.loads(raw))
            # Bind the REQUESTED id to the embedded identity before joining
            # it to any other file in this held store directory.
            if identity.run_id != run_id:
                raise StoreIdMismatchError(run_id, identity.run_id)
            _validate_canonical_run_id(root, identity)
            view = journal_module.replay(store_dir, run_id=run_id, _dir_fd=store_fd)
            custody.verify_directory_identity(store_dir, store_fd, run_id=run_id)
            return cls(store_dir, identity, _view=view)
        finally:
            os.close(store_fd)

    @contextmanager
    def _bound_store(self) -> Iterator[int]:
        """Open and re-bind this object to immutable run.json under one FD."""
        fd = custody.open_directory(
            self.dir,
            create=False,
            run_id=self.identity.run_id,
            purpose="run-state store",
        )
        if fd is None:
            raise UnknownRunError(self.identity.run_id)
        try:
            raw = custody.read_named_bytes(
                self.dir,
                fd,
                RUN_FILENAME,
                run_id=self.identity.run_id,
                purpose="immutable run.json identity",
                allow_missing=True,
            )
            if raw is None:
                raise UnknownRunError(self.identity.run_id)
            observed = RunIdentity.model_validate(json.loads(raw))
            if observed.run_id != self.identity.run_id:
                raise StoreIdMismatchError(self.identity.run_id, observed.run_id)
            _validate_canonical_run_id(self.dir.parent, observed)
            if observed != self.identity:
                raise StoreCustodyError(
                    self.identity.run_id,
                    "immutable run.json content changed after this RunStore was opened",
                )
            yield fd
            custody.verify_directory_identity(self.dir, fd, run_id=self.identity.run_id)
        finally:
            os.close(fd)

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

    @property
    def seq(self) -> int:
        """Sequence number of the currently verified in-memory journal view."""
        return self._view.records[-1].seq if self._view.records else 0

    def has_journal(self) -> bool:
        """Check the journal name under the same store/identity custody."""
        with self._bound_store() as fd:
            return custody.name_exists(
                self.dir,
                fd,
                journal_module.JOURNAL_FILENAME,
                run_id=self.identity.run_id,
                purpose="journal.jsonl authority",
            )

    def load_projection(self) -> journal_module.Projection:
        """Load current.json while bound to this store's immutable identity."""
        with self._bound_store() as fd:
            return journal_module.load_projection(
                self.dir,
                run_id=self.identity.run_id,
                _dir_fd=fd,
            )

    def refresh(self) -> None:
        """Re-read the journal (after another process appended)."""
        with self._bound_store() as fd:
            self._view = journal_module.replay(
                self.dir,
                run_id=self.identity.run_id,
                _dir_fd=fd,
            )

    def status(
        self,
        *,
        now_epoch: int,
        boot_id_now: str,
        proc_root: Path | None = None,
    ) -> RunStatus:
        with self._bound_store() as fd:
            self._view = journal_module.replay(
                self.dir,
                run_id=self.identity.run_id,
                _dir_fd=fd,
            )
            state = self.state
            beat = hb_module.read(self.dir, _dir_fd=fd)
            lease_class: LeaseClassification | None = None
            if lease_module.owner_exists(self.dir, _store_fd=fd):
                lease_class = lease_module.classify_existing(
                    self.dir,
                    boot_id_now=boot_id_now,
                    proc_root=proc_root,
                    _store_fd=fd,
                )
        hb_class = hb_module.classify(
            beat,
            state,
            now_epoch=now_epoch,
            boot_id_now=boot_id_now,
            proc_root=proc_root,
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
        with self._bound_store() as fd:
            self._view = journal_module.replay(
                self.dir,
                run_id=self.identity.run_id,
                _dir_fd=fd,
            )
            source = self.state
            if source is None:
                raise IllegalTransitionError(
                    self.identity.run_id, "journal has no state yet (GENESIS missing)"
                )
            if owner is not None and owner.boot_id != actor_boot_id:
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
            digest = journal_module.append_record(self.dir, record, _dir_fd=fd)
            self._view = journal_module.replay(
                self.dir,
                run_id=self.identity.run_id,
                _dir_fd=fd,
            )
            projection = journal_module.build_projection(
                self.identity.run_id,
                self._view,
                written_at_epoch=now_epoch,
            )
            journal_module.write_projection(self.dir, projection, _dir_fd=fd)
            return digest

    def pin_manifest(
        self, manifest_sha256: str, *, now_epoch: int, actor_pid: int, actor_boot_id: str
    ) -> str:
        """Record the capture manifest hash the run's evidence is bound to.

        Round-1 review fix (2026-08-23): pinned evidence is bound. Re-pinning
        with the SAME hash is idempotent (returns the existing record hash,
        appends nothing); re-pinning with a DIFFERENT hash is refused with
        PinAlreadyBoundError. Validate_resume already rejected
        manifest/state mismatch on resume; this fixes the in-place swap
        that could otherwise launder a new manifest through the journal.
        """
        with self._bound_store() as fd:
            self._view = journal_module.replay(
                self.dir,
                run_id=self.identity.run_id,
                _dir_fd=fd,
            )
            existing = self.pinned_manifest_sha256
            if existing is not None and existing != manifest_sha256:
                raise PinAlreadyBoundError(self.identity.run_id, existing, manifest_sha256)
            if existing == manifest_sha256:
                for prior in reversed(self._view.records):
                    if prior.kind == "MANIFEST_PINNED":
                        return prior.record_sha256
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
            digest = journal_module.append_record(self.dir, record, _dir_fd=fd)
            self._view = journal_module.replay(
                self.dir,
                run_id=self.identity.run_id,
                _dir_fd=fd,
            )
            projection = journal_module.build_projection(
                self.identity.run_id,
                self._view,
                written_at_epoch=now_epoch,
            )
            journal_module.write_projection(self.dir, projection, _dir_fd=fd)
            return digest

    def validate_resume(self, observed_manifest_sha256: str) -> None:
        """Refuse to resume against a manifest the journal never pinned.

        A mismatch is an incident, not an input: the manifest is derived
        evidence and hand-repair is prohibited (runbook §manifest-repair).
        """
        self.refresh()
        pinned = self.pinned_manifest_sha256
        if pinned is None:
            raise ManifestMismatchError(
                self.identity.run_id, "<never-pinned>", observed_manifest_sha256
            )
        if pinned != observed_manifest_sha256:
            raise ManifestMismatchError(self.identity.run_id, pinned, observed_manifest_sha256)

    def write_heartbeat(self, beat: hb_module.Heartbeat) -> None:
        with self._bound_store() as fd:
            hb_module.write(self.dir, beat, _dir_fd=fd)

    def _rewrite_projection(self, now_epoch: int) -> None:
        with self._bound_store() as fd:
            self._view = journal_module.replay(
                self.dir,
                run_id=self.identity.run_id,
                _dir_fd=fd,
            )
            projection = journal_module.build_projection(
                self.identity.run_id, self._view, written_at_epoch=now_epoch
            )
            journal_module.write_projection(self.dir, projection, _dir_fd=fd)

    def rebuild_projection(self, *, now_epoch: int) -> None:
        """Writer-only repair of a torn projection from the journal."""
        self._rewrite_projection(now_epoch)
