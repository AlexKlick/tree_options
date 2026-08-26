"""The production G4 runner wiring: machinery configured by the PROTOCOL-
DECLARED repo calendar (0.2.1 ratification, owner decision 2026-08-26).

PR A left ``verified_inputs.RUNNER_REGISTRY`` empty by design ("Production
PR A registers nothing — no runner is wired — so every packet build refuses
until the owner wires machinery"). The ratified calendar decision fills
exactly that gap: the sealed run consumes the calendar the PROTOCOL
declares (``research_protocol.yaml`` ``calendar:`` → the committed,
checksummed NYSE session fixture), so the machinery registration binds that
declaration — the configuration digest is computed over the PROTOCOL-
DECLARED relative paths plus the sha256 of the fixture bytes (never an
absolute checkout path, so two checkouts of the same head bind the SAME
configuration and keep sharing a content identity), and the registering
layer's digest function re-derives it from the LIVE instance, so calendar
drift between approval and execution is a refusal, never a silent
session-arithmetic change.

The evaluation callable itself stays FAIL-CLOSED. The sealed event is
one-shot at an owner-declared head (docs/m4-g4-sealed-gate-plan.md §3) and
its gate machinery is authored with that event; the closeout runbook §4.6
prohibits execute and prohibits wiring a runner into the CLI execute path.
This registration exists so a verified packet can bind the machinery
IDENTITY (qualified name, code-file hash, calendar configuration digest);
invoking the machinery before the sealed-event evaluation is authored
refuses, and the ``g4_seal`` CLI execute path remains unwired.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tree_options.data.digest import canonical_bytes, sha256_hex
from tree_options.protocol.loader import load_protocol
from tree_options.schemas.common import StrictModel
from tree_options.seal.errors import SealError
from tree_options.seal.identity import RUNNER_VERSION
from tree_options.seal.verified_inputs import (
    HeldVerifiedSealedInputs,
    RegisteredRunner,
    register_runner,
)
from tree_options.time.calendar import StaticSessionCalendar

CALENDAR_RUNNER_CONFIG_DOMAIN = b"tree-options-g4-runner-calendar-config-v1"
PROTOCOL_NAME = "research_protocol.yaml"


class CalendarRunnerConfig(StrictModel):
    """The machinery configuration one packet binds: the protocol-declared
    calendar fixture by its DECLARED relative paths plus the sha256 of the
    fixture bytes — checkout-independent, so the same head binds the same
    configuration everywhere."""

    fixture: str
    checksum_file: str
    fixture_sha256: str


@dataclass(frozen=True)
class ProtocolCalendarBinding:
    """The protocol-declared calendar, resolved and content-verified."""

    repo_root: Path
    fixture: str
    checksum_file: str
    fixture_sha256: str

    @property
    def fixture_path(self) -> Path:
        return self.repo_root / self.fixture

    @property
    def checksum_path(self) -> Path:
        return self.repo_root / self.checksum_file


def protocol_calendar_binding(repo_root: Path) -> ProtocolCalendarBinding:
    """Resolve the calendar the protocol declares and verify its integrity.

    Loads ``research_protocol.yaml`` through the real loader (fail-closed on
    a protocol that does not load), requires the declared implementation to
    be exactly ``static_json``, then constructs the real
    StaticSessionCalendar over the declared fixture + checksum file — a
    fixture that is missing, checksum-mismatched, unordered, or carries an
    early close that is not a session refuses here, never at seal time.
    """
    repo_root = Path(repo_root)
    protocol = load_protocol(repo_root / PROTOCOL_NAME)
    declared = protocol.calendar
    if declared.implementation != "static_json":
        raise SealError(
            "CALENDAR_WIRING_REFUSED",
            f"protocol declares calendar implementation {declared.implementation!r};"
            " the sealed-run wiring binds the static_json repo calendar only",
        )
    fixture_path = repo_root / declared.fixture
    checksum_path = repo_root / declared.checksum_file
    # Fail-closed fixture integrity (checksum, shape, ordering, early closes).
    StaticSessionCalendar(fixture_path, checksum_path)
    return ProtocolCalendarBinding(
        repo_root=repo_root,
        fixture=declared.fixture,
        checksum_file=declared.checksum_file,
        fixture_sha256=sha256_hex(fixture_path.read_bytes()),
    )


def calendar_config_digest(config: CalendarRunnerConfig) -> str:
    return sha256_hex(CALENDAR_RUNNER_CONFIG_DOMAIN + canonical_bytes(config))


class RepoCalendarSealedRunner:
    """The G4 sealed-run machinery, configured by the protocol-declared repo
    calendar. Registration binds this configuration into every verified
    packet; the evaluation callable is fail-closed until the sealed-event
    gate machinery is authored at the owner-declared head."""

    runner_version: str = RUNNER_VERSION

    def __init__(self, binding: ProtocolCalendarBinding) -> None:
        self._binding = binding

    @property
    def calendar_binding(self) -> ProtocolCalendarBinding:
        return self._binding

    def config(self) -> CalendarRunnerConfig:
        return CalendarRunnerConfig(
            fixture=self._binding.fixture,
            checksum_file=self._binding.checksum_file,
            fixture_sha256=self._binding.fixture_sha256,
        )

    def config_digest(self) -> str:
        return calendar_config_digest(self.config())

    def __call__(self, inputs: HeldVerifiedSealedInputs) -> str:
        raise SealError(
            "RUNNER_NOT_AUTHORED",
            "the G4 sealed-event evaluation machinery is not authored: the"
            " sealed run executes ONCE at an owner-declared head"
            " (docs/m4-g4-sealed-gate-plan.md §3) and its gate machinery is"
            " authored with that event — this registration binds the"
            " calendar configuration a verified packet consumes, it never"
            " executes (docs/m4-closeout-runbook.md §4.6: EXECUTE IS"
            " PROHIBITED; the g4_seal CLI execute path wires no runner)",
        )


def wire_production_runner(repo_root: Path) -> RegisteredRunner:
    """The owning-layer call PR A left open: register the calendar-bound
    sealed-run machinery for RUNNER_VERSION. Re-wiring over an identical
    calendar re-registers the identical configuration digest."""
    runner = RepoCalendarSealedRunner(protocol_calendar_binding(repo_root))
    return register_runner(
        runner,
        config_digest=runner.config_digest(),
        # mypy: the registry types the implementation as a bare callable;
        # the production implementation is always a RepoCalendarSealedRunner.
        config_digest_fn=lambda impl: calendar_config_digest(
            impl.config()  # type: ignore[attr-defined]
        ),
    )


__all__ = [
    "CALENDAR_RUNNER_CONFIG_DOMAIN",
    "CalendarRunnerConfig",
    "ProtocolCalendarBinding",
    "RepoCalendarSealedRunner",
    "calendar_config_digest",
    "protocol_calendar_binding",
    "wire_production_runner",
]
