"""Seal error family: every refusal names its code AND its CLI exit code.

The seal is a one-shot authority: a refusal is evidence about where the
authority stands, not an obstacle to route around. Each error carries the
``exit_code`` the g4_seal CLI maps it to, so the library refusal and the
operator-visible exit are one contract (never two tables to keep in sync).
"""

from __future__ import annotations


class SealError(RuntimeError):
    """Base: code + detail + the exit code the CLI maps this refusal to."""

    exit_code = 3  # the ledger-access default; authority paths override

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class VerifiedInputsError(SealError):
    """A typed G4 input or its filesystem custody failed verification."""

    exit_code = 2

    def __init__(self, component: str, detail: str) -> None:
        self.component = component
        super().__init__(
            "VERIFIED_INPUTS_REFUSED",
            f"{component}: {detail}; no verified packet was emitted and no G4 "
            "authority was consumed",
        )


class LedgerRootRefusedError(SealError):
    """A host rule made mechanical: authority may never live under /tmp.

    /tmp is wiped on reboot; seal authority that lived there would silently
    vanish (the 2026-08-22 reboot already orphaned the capture era once —
    see the runstate module docstring). The refusal is a resolved-path prefix
    check, not a judgement call.
    """

    exit_code = 3

    def __init__(self, root: str, detail: str) -> None:
        super().__init__(
            "LEDGER_ROOT_REFUSED",
            f"ledger root {root}: {detail}; /tmp is wiped on reboot — seal "
            "authority must live in durable storage (default: artifacts/g4-authority)",
        )


class LedgerCorruptError(SealError):
    """The hash chain failed somewhere it may not be tolerated."""

    exit_code = 3

    def __init__(self, detail: str) -> None:
        super().__init__(
            "LEDGER_CORRUPT",
            f"{detail}; the authority ledger is evidence and is never "
            "auto-repaired — reconcile with the owner before any further action",
        )


class ApprovalInvalidError(SealError):
    """No approval record recomputes to this run's identity (exit 6)."""

    exit_code = 6

    def __init__(self, sealed_run_id: str, detail: str) -> None:
        super().__init__(
            "APPROVAL_INVALID",
            f"sealed run {sealed_run_id[:12]}…: {detail}; the ledger never "
            "approved THIS sealed content — an approval is consumed only when "
            "its own payload recomputes to the presented identity",
        )


class SecondExecutionRefusedError(SealError):
    """This sealed content was already consumed once (exit 7)."""

    exit_code = 7

    def __init__(self, sealed_run_id: str, detail: str) -> None:
        super().__init__(
            "SECOND_EXECUTION_REFUSED",
            f"sealed run {sealed_run_id[:12]}…: {detail}; the seal is one-shot "
            "per sealed content — a crash after consumption is "
            "RECONCILIATION_REQUIRED, never a re-run",
        )
