"""0.2.1 calendar landing (owner decision 2026-08-26).

Three pinned surfaces:
* the production runner wiring (tree_options.seal.runner) binds the
  PROTOCOL-DECLARED repo calendar into the machinery registry — the
  configuration digest covers the declared fixture paths + fixture bytes,
  is checkout-independent, and is re-derived from the live instance;
* the authored CalendarDecisionArtifact (data/g4/calendar-decision.json)
  validates, decides repo-generated-calendar, and its rationale records
  facts that are TRUE against the committed calendar fixture;
* g4_seal preflight wires the production machinery only when the registry
  is empty (it never hijacks an existing registration) and the CLI execute
  path stays unwired (runbook §4.6: EXECUTE IS PROHIBITED).
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import shutil
import sys
import uuid
from datetime import date
from hashlib import sha256
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import g4_seal  # noqa: E402
from tests.unit.test_g4_verified_inputs import (  # noqa: E402
    TEST_RUNNER_CONFIG_DIGEST,
    clean_git_runner,
    write_valid_inputs,
)
from tree_options.seal import verified_inputs as vi  # noqa: E402
from tree_options.seal.errors import SealError  # noqa: E402
from tree_options.seal.identity import RUNNER_VERSION  # noqa: E402
from tree_options.seal.runner import (  # noqa: E402
    CalendarRunnerConfig,
    RepoCalendarSealedRunner,
    calendar_config_digest,
    protocol_calendar_binding,
    wire_production_runner,
)
from tree_options.seal.verified_inputs import CalendarDecisionArtifact  # noqa: E402
from tree_options.time.calendar import StaticSessionCalendar  # noqa: E402
from tree_options.time.sessions import early_close_instant  # noqa: E402

CALENDAR_JSON = REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json"
CALENDAR_SHA256 = REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.sha256"
DECISION_PATH = REPO_ROOT / "data" / "g4" / "calendar-decision.json"
HOLIDAY_FRIDAYS = ("2025-04-18", "2025-07-04", "2026-04-03", "2026-06-19", "2026-07-03")
EARLY_CLOSE_FRIDAYS = ("2024-11-29", "2025-11-28")
WEEKDAY_EARLY_CLOSES = ("2024-12-24", "2025-07-03", "2025-12-24")
WINDOW_A = (
    "2026-05-08",
    "2026-05-15",
    "2026-05-22",
    "2026-05-29",
    "2026-06-05",
    "2026-06-12",
    "2026-06-26",
    "2026-07-10",
    "2026-07-17",
    "2026-07-24",
    "2026-07-31",
    "2026-08-07",
    "2026-08-14",
)


@pytest.fixture(autouse=True)
def _clean_registry():
    vi.RUNNER_REGISTRY.clear()
    try:
        yield
    finally:
        vi.RUNNER_REGISTRY.clear()


@pytest.fixture()
def ledger_root():
    # Authority roots may not live under pytest's /tmp tree (the ledger
    # refuses /tmp roots); this ignored repo-local scratch dir is test-only.
    root = REPO_ROOT / "artifacts" / "g4-seal-tests" / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---- the wiring ---------------------------------------------------------------------


def test_wire_production_runner_binds_the_protocol_declared_calendar() -> None:
    entry = wire_production_runner(REPO_ROOT)
    binding = protocol_calendar_binding(REPO_ROOT)
    assert binding.fixture == "data/calendar/nyse_sessions_2018_01_02_2026_12_31.json"
    assert binding.checksum_file == "data/calendar/nyse_sessions_2018_01_02_2026_12_31.sha256"
    assert binding.fixture_sha256 == sha256(CALENDAR_JSON.read_bytes()).hexdigest()
    expected = calendar_config_digest(
        CalendarRunnerConfig(
            fixture=binding.fixture,
            checksum_file=binding.checksum_file,
            fixture_sha256=binding.fixture_sha256,
        )
    )
    assert entry.config_digest == expected
    assert entry.implementation_qualname.endswith("RepoCalendarSealedRunner")
    assert entry.runner_version == RUNNER_VERSION
    # the registry holds it for the packet builder to bind
    assert vi.RUNNER_REGISTRY[RUNNER_VERSION] is entry


def test_calendar_config_digest_is_checkout_independent(tmp_path: Path) -> None:
    """The digest covers the DECLARED relative paths + fixture bytes, never
    an absolute checkout path: two checkouts of the same head bind the same
    configuration (and keep sharing a content identity)."""
    binding = protocol_calendar_binding(REPO_ROOT)
    other_root = dataclasses.replace(binding, repo_root=tmp_path)
    assert (
        RepoCalendarSealedRunner(other_root).config_digest()
        == RepoCalendarSealedRunner(binding).config_digest()
    )
    assert str(REPO_ROOT) not in json.dumps(
        json.loads(RepoCalendarSealedRunner(binding).config().model_dump_json())
    )


def test_live_config_digest_sees_calendar_drift() -> None:
    """The registering layer's digest function reads the LIVE instance: a
    mutated calendar binding recomputes to a different digest — calendar
    drift after approval is a refusal at the effect boundary, never
    silent."""
    instance = RepoCalendarSealedRunner(protocol_calendar_binding(REPO_ROOT))
    entry = vi.register_runner(
        instance,
        config_digest=instance.config_digest(),
        config_digest_fn=lambda impl: calendar_config_digest(impl.config()),
    )
    drifted = RepoCalendarSealedRunner(
        dataclasses.replace(instance.calendar_binding, fixture_sha256="0" * 64)
    )
    assert entry.config_digest_fn(drifted) != entry.config_digest


def test_machinery_is_authored_and_fails_closed_on_an_unresolvable_head(
    tmp_path: Path,
) -> None:
    """The evaluation callable IS the authored sealed-event machinery (lane
    m4/g4-sealed-machinery-20260829): RUNNER_NOT_AUTHORED is gone, and the
    first fail-closed act is resolving the declared head — a repo with no
    git identity refuses BEFORE any input byte is read. The full delegation
    (held bundle -> trials -> criteria -> outcome string) is proven on a
    fixture world in test_g4_event_machinery.py; the approval enforcement is
    unchanged (execute_sealed_run's cross-join, test_g4_seal.py)."""
    repo = tmp_path / "not-a-git-repo"
    repo.mkdir()
    for relative in (
        Path("research_protocol.yaml"),
        Path("data/calendar/nyse_sessions_2018_01_02_2026_12_31.json"),
        Path("data/calendar/nyse_sessions_2018_01_02_2026_12_31.sha256"),
    ):
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative, destination)
    instance = RepoCalendarSealedRunner(protocol_calendar_binding(repo))
    with pytest.raises(SealError, match="SEALED_HEAD_UNRESOLVABLE"):
        instance(None)  # type: ignore[arg-type]


# ---- preflight wiring ----------------------------------------------------------------


def test_preflight_wires_production_machinery_only_when_the_registry_is_empty(
    tmp_path: Path, ledger_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = write_valid_inputs(tmp_path)
    args = [
        "--repo",
        str(fixture.paths.repo),
        "--ledger-root",
        str(ledger_root),
        "--lane1-manifest",
        str(fixture.lane1_manifest),
        "--lane1-source",
        str(fixture.lane1_source),
        "--lane2-manifest",
        str(fixture.lane2_manifest),
        "--calendar-decision-artifact",
        str(fixture.calendar),
    ]
    # empty registry: preflight wires the production calendar machinery and
    # the packet binds its configuration digest
    assert g4_seal.cmd_preflight(args, git_runner=clean_git_runner) == 0
    entry = vi.RUNNER_REGISTRY[RUNNER_VERSION]
    assert entry.implementation_qualname.endswith("RepoCalendarSealedRunner")
    payload = json.loads(capsys.readouterr().out)
    assert payload["criteria_inputs"]["runner"]["available"] is True
    assert entry.config_digest in payload["criteria_inputs"]["runner"]["evidence"]

    # a pre-existing registration is NEVER replaced (a test's stub, or the
    # sealed event's own machinery, stays authoritative). Round-5: even a
    # test stub carries the registration contract — a callable preflight()
    # — because register_runner refuses anything less (no preflightless
    # runner, stub or production, can ever hold authority)
    class _Stub:
        runner_version = RUNNER_VERSION

        def preflight(self) -> None:  # the registration contract, no-op here
            return None

        def __call__(self, inputs):  # pragma: no cover - never called
            return "stub"

    stub = _Stub()
    vi.register_runner(
        stub,
        config_digest=TEST_RUNNER_CONFIG_DIGEST,
        config_digest_fn=lambda _impl: TEST_RUNNER_CONFIG_DIGEST,
    )
    assert g4_seal.cmd_preflight(args, git_runner=clean_git_runner) == 0
    assert vi.RUNNER_REGISTRY[RUNNER_VERSION].implementation is stub


def test_execute_cli_stays_unwired() -> None:
    """Runbook §4.6: the CLI execute path refuses before touching the
    ledger, unchanged by the preflight wiring."""
    assert g4_seal.cmd_execute(["--ledger-root", "artifacts/g4-authority"]) == 2


# ---- the authored calendar decision artifact -----------------------------------------


def test_committed_calendar_decision_artifact_validates_and_decides() -> None:
    artifact = CalendarDecisionArtifact.model_validate_json(DECISION_PATH.read_text("utf-8"))
    assert artifact.decision == "repo-generated-calendar"
    assert artifact.owner_decision_id == "m4-protocol-021-ratification-2026-08-26"
    assert artifact.decided_at.isoformat() == "2026-08-26T00:00:00+00:00"
    rationale = artifact.rationale
    for token in (
        "43b0b040ea3c",
        "145",
        "BAR_SESSION_STAMP_MISMATCH",
        *HOLIDAY_FRIDAYS,
        *EARLY_CLOSE_FRIDAYS,
        *WEEKDAY_EARLY_CLOSES,
        "Tuesday",
    ):
        assert token in rationale, f"the rationale must record {token!r}"


# ---- the facts the rationale records are TRUE against the committed fixture -----------


def test_holiday_fridays_are_non_sessions_and_explain_the_145_expected_findings() -> None:
    calendar = StaticSessionCalendar(CALENDAR_JSON, CALENDAR_SHA256)
    for iso in HOLIDAY_FRIDAYS:
        day = date.fromisoformat(iso)
        assert day.weekday() == 4, f"{iso} is not a Friday"
        assert not calendar.is_session(day), f"{iso} must be a NON-session"
    # 5 holiday Fridays x 29 underlyings = the census's 145 EXPECTED findings
    assert len(HOLIDAY_FRIDAYS) * 29 == 145


def test_early_close_sessions_stamp_1300_and_window_a_intersects_them_empty() -> None:
    calendar = StaticSessionCalendar(CALENDAR_JSON, CALENDAR_SHA256)
    early_closes = {
        date.fromisoformat(iso)
        for iso in json.loads(CALENDAR_JSON.read_text("utf-8"))["early_close_sessions"]
    }
    for iso in (*EARLY_CLOSE_FRIDAYS, *WEEKDAY_EARLY_CLOSES):
        day = date.fromisoformat(iso)
        assert day in early_closes, f"{iso} must be an early-close session"
        # the disclosed fail-closed class: the calendar's session close is
        # 13:00 ET, so a vendor bar stamped 16:00 fails BAR_SESSION_STAMP_MISMATCH
        assert calendar.session_close(day) == early_close_instant(day)
    for iso in WINDOW_A:
        day = date.fromisoformat(iso)
        assert calendar.is_session(day), f"window A date {iso} must be a session"
        assert day not in early_closes, f"window A date {iso} must not be an early close"


def test_monday_holidays_roll_the_next_session_to_tuesday() -> None:
    """The Tuesday-rollover path the rationale records: for every Monday
    holiday, the session following the prior Friday is the TUESDAY opening
    the week — a weekend-only wall would miscount the Monday as a session.
    In the committed fixture consecutive sessions jump Friday -> Tuesday
    (a 4-day gap) exactly at Monday holidays (MLK, Presidents' Day,
    Memorial Day, Labor Day: ~4 per year)."""
    calendar = StaticSessionCalendar(CALENDAR_JSON, CALENDAR_SHA256)
    rollovers = 0
    for friday, following in itertools.pairwise(calendar.sessions()):
        if friday.weekday() != 4 or (following - friday).days != 4:
            continue
        monday = friday.toordinal() + 3
        assert not calendar.is_session(date.fromordinal(monday)), (
            f"the 4-day gap after {friday} must be a Monday holiday"
        )
        assert following.weekday() == 1, f"expected the Tuesday after {friday}"
        assert calendar.nth_after(friday, 1) == following
        rollovers += 1
    assert rollovers >= 30, "the fixture carries ~4 Monday holidays per year"
