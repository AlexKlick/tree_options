"""The production G4 runner wiring: machinery configured by the PROTOCOL-
DECLARED repo calendar (0.2.1 ratification, owner decision 2026-08-26).

PR A left ``verified_inputs.RUNNER_REGISTRY`` empty by design ("Production
PR A registers nothing — no runner is wired — so every packet build refuses
until the owner wires machinery"). The ratified calendar decision filled
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

The evaluation callable IS the authored sealed-event machinery (lane
``m4/g4-sealed-machinery-20260829``): it consumes the held verified-inputs
bundle and delegates to ``tree_options.trials.g4_event`` (the lane worlds +
the null trials) and ``tree_options.seal.g4_gate`` (the six pre-declared
criteria, verdict recorded verbatim). The EVENT PROCEDURE stands: machinery
authored → the owner DECLARES the head → the owner APPROVES the packet →
``g4_seal execute`` runs ONCE. Execute without an owner approval record
still refuses (``execute_sealed_run``'s approval cross-join — unchanged),
the one-shot discipline refuses any reuse of the sealed registry or
artifacts, and the ``g4_seal`` CLI execute path stays unwired exactly as
the closeout runbook §4.6 requires.
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
    packet; the evaluation callable runs the AUTHORED sealed-event
    machinery (event procedure: machinery authored → owner declares the
    head → owner approves the packet → execute ONCE)."""

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
        """THE sealed-event evaluation: run the authored machinery over the
        HELD verified-inputs bundle and return the outcome string.

        The machinery (``tree_options.trials.g4_event`` — the two lane
        worlds + the null trials — and ``tree_options.seal.g4_gate`` — the
        six pre-declared criteria, verdict recorded verbatim, evidence
        triple + stamped payloads) consumes the SAME immutable held-byte
        bundle that passed ``verify_sealed_inputs``; no original input path
        is re-read (held bytes are materialized verbatim into a scratch the
        fail-closed manifest verify re-runs over).

        The event procedure this callable closes (runbook §4.6): machinery
        authored (this module) -> the owner DECLARES the head -> the owner
        APPROVES the packet -> ``g4_seal execute`` runs ONCE. Execute
        without an owner approval record still refuses — that enforcement
        lives in ``execute_sealed_run`` and is NOT weakened here: this
        callable is reachable only through it (or a test driving the
        library directly), never through the CLI, and the one-shot
        discipline (existing registry/artifacts refuse) plus the approval
        cross-join stand unchanged.
        """
        import subprocess

        from tree_options.seal.g4_gate import (
            evaluate_and_record,
            preflight_gate_auxiliaries,
            production_gate_paths,
        )
        from tree_options.seal.identity import sealed_run_id
        from tree_options.seal.verified_inputs import identity_from_packet
        from tree_options.trials.g4_event import run_g4_sealed_event

        repo = self._binding.repo_root
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if head.returncode != 0:
            raise SealError(
                "SEALED_HEAD_UNRESOLVABLE",
                f"git rev-parse HEAD failed in {repo}: {head.stderr.strip()[:120]}",
            )
        run_id = sealed_run_id(identity_from_packet(inputs.packet))
        gate_paths = production_gate_paths(repo)
        # BEFORE anything the event creates (round-3 P0): an auxiliary gate
        # input that would raise at evaluation time — an unloadable
        # registry, an unparseable report — refuses here, so it can never
        # burn the sealed workspace (or, under the seal, the CONSUMPTION)
        # without a verdict
        preflight_gate_auxiliaries(paths=gate_paths, repo_root=repo)
        run = run_g4_sealed_event(
            inputs,
            repo_root=repo,
            registry_path=gate_paths.registry,
            artifacts_dir=gate_paths.artifacts_dir,
            scratch_root=gate_paths.scratch_root,
            spot_v2_path=(
                gate_paths.spot_proxy_v2
                if gate_paths.spot_proxy_v2 is not None and gate_paths.spot_proxy_v2.is_file()
                else None
            ),
        )
        evaluation = evaluate_and_record(
            run,
            inputs,
            paths=gate_paths,
            repo_root=repo,
            head=head.stdout.strip(),
            log_lines=(
                f"SEALED_RUN_ID={run_id}",
                f"SEALED_HEAD={head.stdout.strip()}",
                *run.log_lines,
            ),
        )
        return (
            f"{run.run_id} sealed_run_id={run_id} verdict={evaluation.verdict}"
            f" evidence={gate_paths.evidence_root / 'm4-g4-sealed-gate.json'}"
            f" artifacts={run.artifacts_dir}"
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
