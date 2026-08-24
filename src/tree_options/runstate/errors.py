"""Run-state error family: shared by the journal, lease, and store modules.

Every refusal names its code so an operator reading a log can tell a
duplicate launcher (LEASE_HELD) from a corrupted journal (JOURNAL_CORRUPT)
from an illegal lifecycle move (ILLEGAL_TRANSITION) without archaeology.
"""

from __future__ import annotations


class RunStateError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class IllegalTransitionError(RunStateError):
    def __init__(self, run_id: str, detail: str) -> None:
        super().__init__("ILLEGAL_TRANSITION", f"run {run_id}: {detail}")


class LeaseHeldError(RunStateError):
    def __init__(self, run_id: str, detail: str) -> None:
        super().__init__(
            "LEASE_HELD",
            f"run {run_id}: lease held by a live owner ({detail}); "
            "refusing a second launcher — investigate before adopting",
        )


class JournalCorruptError(RunStateError):
    def __init__(self, run_id: str, detail: str) -> None:
        super().__init__(
            "JOURNAL_CORRUPT",
            f"run {run_id}: {detail}; a mid-file journal record failed "
            "verification — the store is evidence and is never auto-repaired",
        )


class ProjectionTornError(RunStateError):
    def __init__(self, run_id: str, detail: str) -> None:
        super().__init__(
            "PROJECTION_TORN",
            f"run {run_id}: {detail}; the projection can be rebuilt from the "
            "journal, but only a caller that is allowed to WRITE may do it "
            "(a status command reports instead)",
        )


class UnknownRunError(RunStateError):
    def __init__(self, run_id: str) -> None:
        super().__init__("UNKNOWN_RUN", f"no run-state store for {run_id!r}")


class StoreExistsError(RunStateError):
    def __init__(self, run_id: str) -> None:
        super().__init__(
            "STORE_EXISTS",
            f"a store for {run_id!r} already exists; open it — never recreate "
            "over a live run's evidence",
        )


class ManifestMismatchError(RunStateError):
    def __init__(self, run_id: str, pinned: str, observed: str) -> None:
        super().__init__(
            "MANIFEST_MISMATCH",
            f"run {run_id}: pinned capture manifest {pinned[:12]}… but the "
            f"on-disk manifest hashes {observed[:12]}…; resume refuses. The "
            "manifest is DERIVED evidence — re-derive it by re-running the "
            "capture (the cache makes that free), never hand-edit it",
        )


class StoreRootRefusedError(RunStateError):
    """A run-state store root resolves under /tmp or is otherwise untrusted.

    /tmp is wiped on reboot — durability there is a lie. Runstate is
    authoritative evidence (the journal, lease, and pinned manifest); it
    MUST live under the repo's durable artifacts/ tree or wherever the
    operator's durable root points. Probe-derived finding (round-1 review,
    2026-08-23): create() and open() accepted /tmp paths and let a launcher
    advance the journal to CAPTURE_COMPLETE there.
    """

    def __init__(self, root: str) -> None:
        super().__init__(
            "STORE_ROOT_REFUSED",
            f"run-state store root {root!r} resolves under /tmp or another "
            "volatile path; /tmp is wiped on reboot and is never the home "
            "of authoritative run-state evidence — point at the repo's "
            "artifacts/runstate (or another durable root)",
        )


class RunIdRefusedError(RunStateError):
    """A run id is not exactly one path component under the validated root.

    Probe-derived finding (round-2 review, 2026-08-23): `root / run_id`
    with an ABSOLUTE run id REPLACES the base (pathlib join semantics), so
    `RunStore.create(root, identity_with_run_id="/tmp/x")` landed the store
    at /tmp/x despite the root passing `_validate_store_root`. Parent-
    bearing ids ("../x") and multi-component ids ("a/b") escape the same
    way. A symlinked run-id directory resolving outside the root is also
    refused (final-resolved-dir descendant check).
    """

    def __init__(self, root: str, run_id: str) -> None:
        super().__init__(
            "RUN_ID_REFUSED",
            f"store root {root!r} refused run id {run_id!r}: a run id must "
            "be exactly one path component under the validated root "
            "(round-2 review, 2026-08-23) — no absolute ids, no parent-"
            "bearing ids, no ids whose resolved directory leaves the root",
        )


class NonCanonicalRunIdError(RunIdRefusedError):
    """The supplied run id is not the digest of its canonical logical core."""

    def __init__(self, root: str, supplied: str, expected: str) -> None:
        RunStateError.__init__(
            self,
            "NONCANONICAL_RUN_ID",
            f"store root {root!r} refused supplied run id {supplied!r}: "
            f"the canonical id for the identity core is {expected!r}; run ids "
            "are computed from campaign, protocol, code, provider, capture "
            "version, universe, arguments, UTC logical start date, and the "
            "optional owner-issued run nonce — never chosen by the operator",
        )


class StoreIdMismatchError(RunStateError):
    """open() was asked for one run id but the store's run.json names another.

    Probe-derived finding (round-3 review, 2026-08-23): a valid run-A store
    placed under directory run-B opened cleanly, so bars-era joins used the
    EMBEDDED identity while runner output used the requested id — one
    execution, two identities. The directory name and identity.run_id are
    one fact; a mismatch is misfiled evidence, never a silent alias.
    """

    def __init__(self, requested: str, stored: str) -> None:
        super().__init__(
            "STORE_ID_MISMATCH",
            f"requested run {requested!r} but the store's run.json names "
            f"{stored!r}: a run store's directory and its identity are one "
            "fact — a mismatch is misfiled evidence, never a silent alias "
            "(round-3 review, 2026-08-23)",
        )


class JournalConcurrentWriteError(RunStateError):
    """append_record's caller-supplied prev_record_sha256 no longer matches
    the file's locked tail under flock.

    Indicates either a stale append (the caller built its record against an
    older replay view) or an interleaved writer (the advisory flock did
    not serialize enough). Append is refused; repair is an explicit owner
    act. Probe-derived finding (round-1 review, 2026-08-23).
    """

    def __init__(self, run_id: str, detail: str) -> None:
        super().__init__(
            "JOURNAL_CONCURRENT_WRITE",
            f"run {run_id}: {detail}; refusing to append a record whose "
            "prev_record_sha256 does not match the locked journal tail. "
            "Re-replay and rebuild the prev hash before appending",
        )


class PinAlreadyBoundError(RunStateError):
    """pin_manifest was called with a DIFFERENT hash after a pin already exists.

    A pinned manifest is bound evidence — the runbook prohibits changing
    it. Re-pinning with the SAME hash is idempotent and returns the existing
    record; a different hash is refused outright. Probe-derived finding
    (round-1 review, 2026-08-23).
    """

    def __init__(self, run_id: str, existing: str, attempted: str) -> None:
        super().__init__(
            "PIN_ALREADY_BOUND",
            f"run {run_id}: capture manifest already pinned to "
            f"{existing[:12]}…; refusing a second pin to {attempted[:12]}… "
            "— pinned evidence is bound (runbook §3); re-derive by re-running "
            "the capture, never swap a pin",
        )
