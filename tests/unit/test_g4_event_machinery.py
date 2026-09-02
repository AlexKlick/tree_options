"""The G4 sealed-event MACHINERY tests (fixture-only; the HARD FIREWALL).

Every world here is a SYNTHETIC fixture capture — the ``era_world`` /
``era_friday_world`` vendor shapes — built through the same fixture
builders the era-twin tests use, sized for the DEFAULT Agenda-D geometry.
The machinery's first execution against the REAL era artifacts is the
sealed event itself and never happens in this file: no real capture dir is
read, no coverage peeked, no criterion dry-run on a real payload.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT
from tests.fixtures.cboe_eod_rows import SPY_MAIN_ROWS, write_csv
from tests.fixtures.massive_structural_sample import (
    bar,
    bars_payload,
    contract_result,
    contracts_payload,
)
from tree_options.data.cboe_eod import build_real_options_manifest, parse_cboe_eod_csv
from tree_options.data.massive_manifest import (
    CAPTURE_MANIFEST_FILENAME,
    build_massive_capture_manifest,
)
from tree_options.data.real_overlay import build_real_overlay
from tree_options.seal.g4_gate import (
    REJECTION_FLOOR,
    G4GatePaths,
    evaluate_and_record,
    evaluate_g4_criteria,
    load_json,
)
from tree_options.seal.runner import RepoCalendarSealedRunner, protocol_calendar_binding
from tree_options.seal.verified_inputs import (
    EXPECTED_MASSIVE_CAPTURE_VERSION,
    SealedInputPaths,
    build_calendar_decision_artifact,
    verify_sealed_inputs,
)
from tree_options.trials.g4_event import (
    AGENDA_D_GEOMETRY,
    G4_SEALED_SEED_LANE1,
    G4_SEALED_SEED_LANE2_ARM_A,
    G4_SEALED_SEED_LANE2_ARM_B,
    build_lane2_world,
    lane2_census_payload,
    run_g4_sealed_event,
    sealed_split_override,
)
from tree_options.trials.null_score import null_score

# the fixture-scale criterion-4 floor (a criterion PARAMETER; the sealed
# default stays the pre-declared 50): 3 = the strict-map test's crafted
# counted total sits below it while the real fixture lanes sit far above
MINI_FLOOR = 3

CAPTURE_FIRST = date(2023, 1, 3)
CAPTURE_LAST = date(2024, 10, 25)
MASTER_AS_OF = date(2024, 6, 14)
SPOT = {"SPY": Decimal("600.00"), "TSLA": Decimal("200.00")}

# One REAL-WIRE-SHAPE 3dp close (the 2026-08-31 sealed event's crash class:
# the vendor serves sub-cent closes — ADBE "417.125" on the real wire): a
# synthetic VALUE in the real precision CLASS, on ONE SPY session, so the
# machinery proves the bar-boundary quantization the sealed run needs. The
# default lane-2 build carries it (the fixture holds the wire shape the
# sealed event tripped on); a caller can pass {} for the flat ≤2dp world.
WIRE_SHAPE_3DP_UNDERLYING = "SPY"
WIRE_SHAPE_3DP_SESSION = date(2024, 6, 14)
WIRE_SHAPE_3DP_TOKEN = "600.125"
THREE_DP_PATCH = {(WIRE_SHAPE_3DP_UNDERLYING, WIRE_SHAPE_3DP_SESSION): WIRE_SHAPE_3DP_TOKEN}

# The synthetic contract plan: (ticker, expiry, strike, bar span, volume, mode)
#   fill       — the FILL contract: ATM, huge volume, the repo pricer's own
#                premium; in-band DTE for the July-Aug 2024 grid decisions
#   tail       — a long-span contract that sets the overlay (and so grid)
#                breadth so the Agenda-D geometry fits and the execution
#                tail has headroom; never DTE-in-band, never picked
#   deep_itm   — premium under intrinsic: MassiveDerivationError cells
#   zero_volume— zero-volume bars (the counted class), then no_bar cells
#                once its bars stop while the master as_of holds its window
#   low_volume — TSLA, below the ratified flow min: session_volume_flow FAIL
_CONTRACTS = (
    (
        "O:SPY240920C00600000",
        date(2024, 9, 20),
        "600",
        (date(2024, 5, 1), date(2024, 8, 30)),
        1_000_000,
        "fill",
        "SPY",
    ),
    (
        "O:SPY250620C00600000",
        date(2025, 6, 20),
        "600",
        (CAPTURE_FIRST, CAPTURE_LAST),
        1_000_000,
        "tail",
        "SPY",
    ),
    (
        "O:SPY240816C00400000",
        date(2024, 8, 16),
        "400",
        (CAPTURE_FIRST, date(2023, 6, 30)),
        1_000,
        "deep_itm",
        "SPY",
    ),
    (
        "O:SPY240920C00700000",
        date(2024, 9, 20),
        "700",
        (CAPTURE_FIRST, date(2023, 6, 30)),
        0,
        "zero_volume",
        "SPY",
    ),
    (
        "O:TSLA240920C00200000",
        date(2024, 9, 20),
        "200",
        (CAPTURE_FIRST, CAPTURE_LAST),
        10,
        "low_volume",
        "TSLA",
    ),
)
# the deliberately-refused master row (an unparseable OCC ticker): the
# counted master-row-refusal class
_BAD_TICKER = "NOTANEFFECTIVEOCCROW"


def _t(session: date) -> str:
    """ms epoch of midnight America/New_York — the vendor bar anchor
    (``massive_options.session_of_epoch_ms`` inverts exactly this)."""
    from tree_options.data.cboe_eod import SESSION_TIMEZONE

    local = datetime.combine(session, time(0), tzinfo=SESSION_TIMEZONE)
    return str(int(local.timestamp() * 1000))


def _bs_call_premium(spot: float, strike: float, expiry: date, session: date) -> str:
    from tree_options.synth_options.greeks import bs_price

    price = bs_price(
        spot=spot,
        strike=strike,
        dte_calendar_days=(expiry - session).days,
        iv=0.18,
        risk_free=0.03,
        dividend_yield=0.0,
        call_put="C",
    )
    return f"{price:.4f}"


def _exchange_sessions() -> tuple[date, ...]:
    from tree_options.data.vwap_pit_surface import repo_exchange_calendar

    calendar = repo_exchange_calendar(REPO_ROOT)
    return tuple(
        session for session in calendar.sessions() if CAPTURE_FIRST <= session <= CAPTURE_LAST
    )


# ---- the fixture bundle (one module-scoped build) --------------------------------


@dataclass(frozen=True)
class MiniGateFixture:
    repo: Path
    held_paths: SealedInputPaths
    era_census: Path
    mutation_report: Path
    spot_v2: Path
    head: str


def _build_fixture_repo(root: Path) -> Path:
    repo = root / "repo"
    for relative in (
        Path(".gitignore"),
        Path("research_protocol.yaml"),
        Path("data/g4/sealed-criteria.json"),
        Path("docs/m4-g4-sealed-gate-plan.md"),
        Path("data/calendar/nyse_sessions_2018_01_02_2026_12_31.json"),
        Path("data/calendar/nyse_sessions_2018_01_02_2026_12_31.sha256"),
        # the LIVE mutation registry: criterion 6 binds the report to it,
        # so the fixture repo (a REAL git repo standing in for the head)
        # carries the registry exactly as the sealed head does
        Path("scripts/mutate.py"),
    ):
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative, destination)
    return repo


def _build_lane1(root: Path) -> tuple[Path, Path]:
    lane1 = root / "lane1"
    lane1.mkdir(parents=True)
    rows = list(SPY_MAIN_ROWS)
    # FIRING parse refusals: malformed field counts (the counted class) —
    # enough that the runner's PRODUCTION path exercises the pre-declared
    # pooled floor of 50 and passes it honestly
    rows.extend(f"SPY,malformed,row,{index}" for index in range(60))
    source = write_csv(lane1 / "spy.csv", rows)
    parsed = parse_cboe_eod_csv(source)
    overlay = build_real_overlay(parsed)
    manifest = build_real_options_manifest(parsed, overlay=overlay)
    manifest_path = lane1 / "capture-manifest.json"
    manifest_path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return source, manifest_path


def _build_lane2(
    root: Path, *, spot_tokens: dict[tuple[str, date], str] | None = None
) -> tuple[Path, Path]:
    """The lane-2 synthetic capture. ``spot_tokens`` overrides per-session
    close tokens (the real-wire 3dp shape by default — the sealed event's
    crash class); ``{}`` builds the flat ≤2dp world."""
    lane2 = root / "lane2"
    masters = lane2 / "masters"
    bars = lane2 / "bars"
    masters.mkdir(parents=True)
    bars.mkdir()
    sessions = _exchange_sessions()

    for underlying in ("SPY", "TSLA"):
        rows = [
            contract_result(
                ticker=ticker,
                underlying=underlying,
                expiration=f"{expiry:%Y-%m-%d}",
                strike=strike,
                contract_type="call",
            )
            for ticker, expiry, strike, _span, _v, _mode, owner in _CONTRACTS
            if owner == underlying
        ]
        if underlying == "SPY":
            rows.append(
                contract_result(
                    ticker=_BAD_TICKER,
                    underlying="SPY",
                    expiration="2024-09-20",
                    strike="600",
                    contract_type="call",
                )
            )
        (masters / f"{underlying}_{MASTER_AS_OF:%Y-%m-%d}.json").write_text(
            contracts_payload(results=tuple(rows), as_of=f"{MASTER_AS_OF:%Y-%m-%d}"),
            encoding="utf-8",
        )

    for ticker, expiry, strike, (lo, hi), volume, mode, underlying in _CONTRACTS:
        span = tuple(session for session in sessions if lo <= session <= hi)
        rows = []
        for session in span:
            if mode == "deep_itm":
                premium = "5.0000"  # < intrinsic 200: the derivation refuses
            elif mode == "zero_volume":
                premium = "2.0000"
            else:
                premium = _bs_call_premium(float(SPOT[underlying]), float(strike), expiry, session)
            v = "0" if mode == "zero_volume" else str(volume)
            rows.append(
                bar(
                    v=v,
                    t=_t(session),
                    vw=premium,
                    o=premium,
                    c=premium,
                    h=f"{Decimal(premium) + Decimal('0.10')}",
                    low=f"{Decimal(premium) - Decimal('0.10')}",
                    n="24",
                )
            )
        (bars / f"bars_{ticker}.json").write_text(
            bars_payload(ticker=ticker, results_count=str(len(rows)), results=tuple(rows)),
            encoding="utf-8",
        )

    spot_json = {
        underlying: {session.isoformat(): f"{value.normalize()}" for session in sessions}
        for underlying, value in SPOT.items()
    }
    tokens = THREE_DP_PATCH if spot_tokens is None else spot_tokens
    for (underlying, session), token in tokens.items():
        spot_json[underlying][session.isoformat()] = token
    (lane2 / "spot_proxy.json").write_text(json.dumps(spot_json), encoding="utf-8")

    manifest = build_massive_capture_manifest(
        lane2,
        capture_version=EXPECTED_MASSIVE_CAPTURE_VERSION,
        budget_limit=45,
        requests_charged=4,
        client_stats={"requests": 4},
        masters=(
            {
                "underlying": underlying,
                "as_of": f"{MASTER_AS_OF:%Y-%m-%d}",
                "pages": 1,
                "rows": 3,
                "complete": True,
                "truncated": False,
                "error": None,
                "file": f"{underlying}_{MASTER_AS_OF:%Y-%m-%d}.json",
            }
            for underlying in ("SPY", "TSLA")
        ),
        bars=(f"bars_{ticker}.json" for ticker, *_ in _CONTRACTS),
        spot_proxy=spot_json,
        notes=("hermetic G4 machinery fixture",),
    )
    manifest_path = lane2 / CAPTURE_MANIFEST_FILENAME
    manifest_path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")

    # the OPTIONAL declared v2 dollar-volume source (20-session medians):
    # SPY 600*100k = $60M (passes the $50M term), TSLA 200*100k = $20M (fails)
    v2_window = [s for s in _exchange_sessions_full() if date(2022, 11, 1) <= s <= CAPTURE_LAST]
    v2_json = {
        underlying: {
            session.isoformat(): {"close": f"{SPOT[underlying].normalize()}", "volume": 100_000}
            for session in v2_window
        }
        for underlying in SPOT
    }
    v2 = root / "spot-proxy-v2.json"
    v2.write_text(json.dumps(v2_json), encoding="utf-8")
    return manifest_path, v2


def _exchange_sessions_full() -> tuple[date, ...]:
    from tree_options.data.vwap_pit_surface import repo_exchange_calendar

    calendar = repo_exchange_calendar(REPO_ROOT)
    return tuple(
        session for session in calendar.sessions() if date(2022, 11, 1) <= session <= CAPTURE_LAST
    )


def _build_era_census(root: Path) -> Path:
    census = {
        "schema_version": "m4-coverage-census/1",
        "coverage": {"expected_masters": 2},
        "values": {"observed_census_fact": {"distinct_contracts": {"v": 6}}},
    }
    path = root / "census.json"
    path.write_text(json.dumps(census), encoding="utf-8")
    return path


def _live_mutation_registry() -> tuple[frozenset[str], str]:
    """The LIVE registry (the authored MUTANTS list + its content digest;
    the JSON artifact is generated output). Criterion 6 binds the fixture
    report to both, exactly as the sealed CLI does — through the gate's own
    loader."""
    from tree_options.seal.g4_gate import live_mutation_registry

    loaded = live_mutation_registry(REPO_ROOT)
    assert loaded is not None, "the live mutation registry must exist at REPO_ROOT"
    return loaded


def _build_mutation_report(root: Path, *, head: str) -> Path:
    """A report shaped like the real one at this head: EVERY live registry
    id KILLED, N/N, restoration green, and the registry DIGEST + HEAD the
    mutation runner stamps — so the fixture exercises criterion 6's binding
    rather than a synthetic single-mutant stub."""
    registry, digest = _live_mutation_registry()
    ids = tuple(sorted(registry))
    report = {
        "mutants": [
            {
                "id": mid,
                "file": (
                    "src/tree_options/seal/g4_gate.py"
                    if "g4" in mid
                    else "src/tree_options/trials/options_run.py"
                ),
                "invariant": f"registry mutant {mid}",
                "verdict": "KILLED",
            }
            for mid in ids
        ],
        "totals": {"KILLED": len(ids)},
        "total": len(ids),
        "restoration_suite_passed": True,
        "registry_digest": digest,
        "head": head,
    }
    path = root / "m0-mutations.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def _build_bundle(
    root: Path, *, spot_tokens: dict[tuple[str, date], str] | None = None
) -> MiniGateFixture:
    """The full fixture bundle: a committed fixture repo (a REAL git repo so
    the machinery's fail-closed stamping runs the sealed discipline), both
    lanes' synthetic captures, and the auxiliary stamped inputs."""
    repo = _build_fixture_repo(root)
    source, lane1_manifest = _build_lane1(root)
    lane2_manifest, spot_v2 = _build_lane2(root, spot_tokens=spot_tokens)
    calendar = root / "calendar-decision.json"
    calendar.write_text(
        build_calendar_decision_artifact(
            decision="repo-generated-calendar",
            owner_decision_id="fixture-owner-decision",
            decided_at=datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
            rationale="Hermetic typed decision for the G4 machinery tests.",
        ).model_dump_json(indent=2)
        + "\n",
        encoding="utf-8",
    )
    era_census = _build_era_census(root)
    # commit the fixture repo FIRST so its head is fixed BEFORE the
    # mutation report stamps it (the production artifacts live under the
    # repo's gitignored artifacts/, so writing them after the commit never
    # moves the head — the report can bind to the head the runner and CLI
    # will rev-parse)
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "fixture@g4.test"],
        ["git", "config", "user.name", "g4 fixture"],
    ):
        subprocess.run(command, cwd=repo, check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "fixture repo for g4 machinery"],
        check=True,
    )
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    mutation_report = _build_mutation_report(root, head=head)
    # the PRODUCTION path set the runner consumes (production_gate_paths)
    production_census = repo / "artifacts" / "census" / "43b0b040ea3c" / "census.json"
    production_census.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(era_census, production_census)
    production_report = repo / "artifacts" / "m0-mutations.json"
    shutil.copyfile(mutation_report, production_report)
    production_v2 = repo / "artifacts" / "spot-proxy-v2.json"
    shutil.copyfile(spot_v2, production_v2)
    return MiniGateFixture(
        repo=repo,
        head=head,
        held_paths=SealedInputPaths(
            repo=repo,
            lane1_manifest=lane1_manifest,
            lane1_source=source,
            lane2_manifest=lane2_manifest,
            calendar_decision_artifact=calendar,
        ),
        era_census=era_census,
        mutation_report=mutation_report,
        spot_v2=spot_v2,
    )


@pytest.fixture(scope="module")
def mini_gate(tmp_path_factory: pytest.TempPathFactory) -> MiniGateFixture:
    """The module's default bundle: carries the real-wire 3dp close (the
    sealed event's crash class) so every end-to-end machinery test in this
    file runs against the wire shape the sealed run tripped on."""
    return _build_bundle(tmp_path_factory.mktemp("g4mach"))


def _run_mini_gate(mini_gate: MiniGateFixture, artifacts: Path, registry: Path, scratch: Path):
    held = verify_sealed_inputs(mini_gate.held_paths)
    return run_g4_sealed_event(
        held,
        repo_root=mini_gate.repo,
        registry_path=registry,
        artifacts_dir=artifacts,
        scratch_root=scratch,
        spot_v2_path=mini_gate.spot_v2,
    )


@pytest.fixture(autouse=True, scope="module")
def _wired_production_runner(mini_gate: MiniGateFixture):
    """The production machinery wiring over the FIXTURE repo (the g4_seal
    preflight's own rule: wire when the registry is empty, never replace).
    Module-scoped so the module's run fixtures see the registration."""
    from tree_options.seal import verified_inputs as vi
    from tree_options.seal.runner import wire_production_runner

    vi.RUNNER_REGISTRY.clear()
    wire_production_runner(mini_gate.repo)
    try:
        yield
    finally:
        vi.RUNNER_REGISTRY.clear()


@pytest.fixture(scope="module")
def mini_run(mini_gate: MiniGateFixture, tmp_path_factory: pytest.TempPathFactory):
    """The primary mini run at the DEFAULT Agenda-D geometry PLUS its clean
    replay (same held bytes, fresh registry/artifacts/scratch): the replay
    is what criterion 5 compares against."""
    primary_root = tmp_path_factory.mktemp("g4mini-primary")
    run = _run_mini_gate(
        mini_gate, primary_root / "artifacts", primary_root / "sealed.db", primary_root / "scratch"
    )
    replay_root = tmp_path_factory.mktemp("g4mini-replay")
    replay = _run_mini_gate(
        mini_gate,
        replay_root / "artifacts",
        replay_root / "sealed.db",
        replay_root / "scratch",
    )
    return mini_gate, run, replay


def _paths_for(mini: MiniGateFixture, run, evidence: Path, replay_dir: Path) -> G4GatePaths:
    return G4GatePaths(
        evidence_root=evidence,
        registry=mini.repo / "artifacts" / "unused.db",
        artifacts_dir=run.artifacts_dir,
        scratch_root=mini.repo / "artifacts" / "unused-scratch",
        era_census=mini.era_census,
        replay_artifacts=replay_dir,
        mutation_report=mini.mutation_report,
    )


def _evaluate(mini_run, evidence: Path, *, replay=True, floor: int = MINI_FLOOR):
    mini, run, replay_run = mini_run
    replay_dir = mini.repo / "artifacts" / "g4-sealed-replay"
    if replay_dir.exists():
        shutil.rmtree(replay_dir)
    if replay:
        shutil.copytree(replay_run.artifacts_dir, replay_dir)
    return (
        evaluate_and_record(
            run,
            verify_sealed_inputs(mini.held_paths),
            paths=_paths_for(mini, run, evidence, replay_dir),
            repo_root=mini.repo,
            head=mini.head,
            mutation_registry_ids=_live_mutation_registry()[0],
            mutation_registry_digest=_live_mutation_registry()[1],
            rejection_floor=floor,
            rejection_lane1_floor=floor,  # the fixture gate keeps lane-1 teeth
        ),
        replay_dir,
    )


# ---- Fix A: the lane-2 null-trial library seam ------------------------------------


def test_a_symlinked_run_workspace_component_refuses(
    mini_gate: MiniGateFixture, tmp_path: Path
) -> None:
    """Codex round 2, P1-3 (verified by probe): a pre-planted directory
    symlink at the run-scoped workspace (``artifacts/g4-sealed-runs/<key>``
    or any component of its chain) would redirect the registry, artifacts,
    scratch, and replay OUTSIDE the checkout while every lexical check
    stays green — the sealed run refuses a symlinked workspace component
    before a single byte is created, naming it."""
    import os

    outside = tmp_path / "escape-target"
    outside.mkdir()
    run_key = "a" * 64
    runs = mini_gate.repo / "artifacts" / "g4-sealed-runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / run_key).symlink_to(outside)
    held = verify_sealed_inputs(mini_gate.held_paths)
    with pytest.raises(RuntimeError, match="symlinked sealed workspace component") as excinfo:
        run_g4_sealed_event(
            held,
            repo_root=mini_gate.repo,
            registry_path=runs / run_key / "g4-sealed.db",
            artifacts_dir=runs / run_key / "artifacts",
            scratch_root=runs / run_key,
            spot_v2_path=mini_gate.spot_v2,
        )
    assert str(runs / run_key) in str(excinfo.value)
    assert os.path.islink(runs / run_key), "the planted link itself stays untouched"
    # a REAL directory at the same shape runs (the guard refuses symlinks,
    # not the run-scoped layout)
    real_key = "b" * 64
    run = run_g4_sealed_event(
        held,
        repo_root=mini_gate.repo,
        registry_path=runs / real_key / "g4-sealed.db",
        artifacts_dir=runs / real_key / "artifacts",
        scratch_root=runs / real_key,
        spot_v2_path=mini_gate.spot_v2,
    )
    assert run.trial_statuses == {("2", "A"): "COMPLETED", ("2", "B"): "COMPLETED"}


def test_the_sealed_lanes_run_end_to_end_on_the_fixture_world(mini_run) -> None:
    """The seam: synthetic capture -> held bundle -> trials -> stamped
    payloads, with ZERO real-artifact reads. Both arms COMPLETE at the
    DEFAULT geometry, the payloads carry the declared seeds and the lane-2
    regime keys, and the stamped fills carry the selected bar's own facts."""
    _mini, run, _replay = mini_run
    assert run.trial_statuses == {("2", "A"): "COMPLETED", ("2", "B"): "COMPLETED"}
    for arm in ("A", "B"):
        body = json.loads(run.trial_payload_paths[("2", arm)].read_text(encoding="utf-8"))
        payload = body["payload"]
        assert payload["liquidity_lane"] == 2
        assert payload["score_seed"] == (
            G4_SEALED_SEED_LANE2_ARM_A if arm == "A" else G4_SEALED_SEED_LANE2_ARM_B
        )
        assert payload["flow_min_session_volume"] == 100
    arm_a = json.loads(run.trial_payload_paths[("2", "A")].read_text(encoding="utf-8"))
    fills = arm_a["payload"]["fills_log"]
    assert fills, "the fixture world must actually fill (arm A)"
    for fill in fills:
        assert fill["bar_session"] is not None
        assert fill["bar_volume"] == 1_000_000
    for row in arm_a["payload"]["pooled"]["positions"][:5]:
        assert row["score"] == pytest.approx(
            null_score(
                seed=G4_SEALED_SEED_LANE2_ARM_A,
                session=date.fromisoformat(row["decision_session"]),
                security_id=row["underlying_security_id"],
            )
        )


def test_the_lane_censuses_stamp_the_strict_class_map_facts(mini_run) -> None:
    _mini, run, _replay = mini_run
    lane2 = json.loads(run.census_payload_paths["lane2"].read_text(encoding="utf-8"))["payload"]
    classes = lane2["rejection_classes"]
    assert classes["zero_volume_bar_refusals"] > 0
    assert classes["massive_derivation_error_refusals"] > 0
    assert classes["master_row_refusals"] == 1
    assert classes["no_bar_not_evaluable_disclosed"] > 0
    declared = lane2["declared_configuration"]
    assert declared["seeds"] == {
        "arm_a": G4_SEALED_SEED_LANE2_ARM_A,
        "arm_b": G4_SEALED_SEED_LANE2_ARM_B,
    }
    assert declared["derivation_provenance"] in declared["accepted_delta_provenance"]
    assert declared["geometry_source"].startswith("Agenda D proposal")
    lane1 = json.loads(run.census_payload_paths["lane1"].read_text(encoding="utf-8"))["payload"]
    # the fixture's own duplicate contract row + the 60 malformed rows
    assert lane1["rejection_classes"]["firing_parse_refusals"] == 61
    assert lane1["declared_configuration"]["seed"] == G4_SEALED_SEED_LANE1


def test_the_declared_geometry_defaults_to_the_agenda_d_proposal() -> None:
    from tree_options.trials.options_run import OptionsSplitOverride

    assert sealed_split_override() == OptionsSplitOverride(
        label_horizon_sessions=5,
        embargo_sessions=2,
        val_sessions=12,
        test_sessions=13,
        roll_sessions=13,
        min_train_sessions=40,
    )
    assert AGENDA_D_GEOMETRY == (5, 2, 12, 13, 13, 40)


def test_the_one_shot_discipline_refuses_a_second_invocation(
    mini_gate: MiniGateFixture, tmp_path: Path
) -> None:
    """The M3 pattern: an existing sealed registry or artifacts dir refuses
    before a single byte is read."""
    held = verify_sealed_inputs(mini_gate.held_paths)
    registry = tmp_path / "sealed.db"
    artifacts = tmp_path / "artifacts"
    _run_via_library = dict(
        held=held,
        repo_root=mini_gate.repo,
        split_override=None,
        spot_v2_path=mini_gate.spot_v2,
    )
    run_g4_sealed_event(
        registry_path=registry,
        artifacts_dir=artifacts,
        scratch_root=tmp_path / "scratch",
        **_run_via_library,  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="refusing to reuse sealed registry"):
        run_g4_sealed_event(
            registry_path=registry,
            artifacts_dir=tmp_path / "artifacts2",
            scratch_root=tmp_path / "scratch2",
            **_run_via_library,  # type: ignore[arg-type]
        )
    with pytest.raises(RuntimeError, match="refusing to reuse sealed artifacts"):
        run_g4_sealed_event(
            registry_path=tmp_path / "sealed2.db",
            artifacts_dir=artifacts,
            scratch_root=tmp_path / "scratch3",
            **_run_via_library,  # type: ignore[arg-type]
        )


# ---- the bar-boundary price quantization (the 2026-08-31 crash class) --------------


def _world_for(mini: MiniGateFixture, scratch: Path):
    """The lane-2 WORLD alone (no trials) from a bundle's verified held
    bytes — the seam the 2026-08-31 sealed event crashed in."""
    from tree_options.protocol.loader import load_protocol_bytes

    held = verify_sealed_inputs(mini.held_paths)
    world = build_lane2_world(
        held,
        repo_root=mini.repo,
        scratch=scratch,
        protocol=load_protocol_bytes(held.protocol_bytes),
        spot_v2_path=mini.spot_v2,
    )
    return held, world


def _census_for(mini: MiniGateFixture, held, world) -> dict[str, object]:
    from tree_options.protocol.loader import load_protocol_bytes

    return lane2_census_payload(
        world,
        held=held,
        protocol=load_protocol_bytes(held.protocol_bytes),
        spot_v2_path=mini.spot_v2,
        split_override=sealed_split_override(),
    )


def test_a_three_decimal_wire_close_quantizes_the_flat_bar_at_the_boundary(
    mini_gate: MiniGateFixture, tmp_path: Path
) -> None:
    """The 2026-08-31 sealed event crashed exactly here (BarRecord
    decimal_max_places on a real-wire 3dp close, ADBE "417.125"): the world
    must BUILD, the flat bar must carry the cent-quantized close in all four
    OHLC fields, and the custody counters must name exactly the rows the
    tick moved."""
    held, world = _world_for(mini_gate, tmp_path / "scratch")
    assert world.spot_close_quantized_rows == 1
    assert world.spot_close_max_quantization_delta == Decimal("0.005")
    bar = next(
        b
        for b in world.dataset.bars
        if b.security_id == WIRE_SHAPE_3DP_UNDERLYING and b.session == WIRE_SHAPE_3DP_SESSION
    )
    # the tie resolves by the decimal context default (ROUND_HALF_EVEN):
    # 600.125 -> 600.12
    assert (bar.open, bar.high, bar.low, bar.close) == (Decimal("600.12"),) * 4
    # nothing dropped: every declared spot row is a bar (2 names x sessions)
    assert len(world.dataset.bars) == 2 * len(_exchange_sessions())
    block = _census_for(mini_gate, held, world)["spot_close_quantization"]
    assert block["rows_quantized"] == 1
    assert block["max_delta"] == "0.005"
    assert block["tick"] == "0.01"
    assert "ROUND_HALF_EVEN" in block["rule"]


def test_the_spot_close_quantization_block_stamps_the_exact_census_values(mini_run) -> None:
    """The custody disclosure is a STAMPED census fact: the wire-shape row
    count, the exact max delta, the tick, and the tie rule travel inside the
    sealed payload the criteria read."""
    _mini, run, _replay = mini_run
    lane2 = json.loads(run.census_payload_paths["lane2"].read_text(encoding="utf-8"))["payload"]
    block = lane2["spot_close_quantization"]
    assert block["rows_quantized"] == 1
    assert block["max_delta"] == "0.005"
    assert block["tick"] == "0.01"
    assert "ROUND_HALF_EVEN" in block["rule"]


def test_only_two_decimal_closes_carry_through_exactly_with_no_custody_noise(
    tmp_path: Path,
) -> None:
    """The quiet path: with only ≤2dp closes nothing is quantized, nothing
    is disclosed, and every flat bar carries its declared close EXACTLY —
    no custody noise, no re-serialization drift."""
    root = tmp_path / "flat"
    root.mkdir()
    mini = _build_bundle(root, spot_tokens={})
    held, world = _world_for(mini, tmp_path / "scratch")
    assert world.spot_close_quantized_rows == 0
    assert world.spot_close_max_quantization_delta is None
    declared = json.loads((root / "lane2" / "spot_proxy.json").read_text(encoding="utf-8"))
    bars = {(bar.security_id, bar.session): bar for bar in world.dataset.bars}
    assert len(bars) == sum(len(rows) for rows in declared.values())
    for underlying, rows in declared.items():
        for session_iso, token in rows.items():
            bar = bars[(underlying, date.fromisoformat(session_iso))]
            assert (bar.open, bar.high, bar.low, bar.close) == (Decimal(token),) * 4
            # REPRESENTATION-exact (Codex round-1 P1): the original Decimal
            # object passes through untouched — an exponent-0 "6E+2" token
            # stays "6E+2", never re-serialized as "600.00"
            assert (str(bar.open), str(bar.high), str(bar.low), str(bar.close)) == (token,) * 4
    block = _census_for(mini, held, world)["spot_close_quantization"]
    assert block["rows_quantized"] == 0
    assert block["max_delta"] is None


def test_multi_row_custody_tracks_the_largest_value_movement(tmp_path: Path) -> None:
    """Two rewritten rows with DIFFERENT deltas (Codex round-1 P2): the
    counter counts both, max_delta is the largest VALUE movement (not the
    last, not the smallest — the aggregation comparator is load-bearing),
    and a non-tie fraction rounds down while the .125 tie resolves to the
    EVEN cent under the explicit ROUND_HALF_EVEN."""
    root = tmp_path / "multi"
    root.mkdir()
    mini = _build_bundle(
        root,
        spot_tokens={
            (WIRE_SHAPE_3DP_UNDERLYING, WIRE_SHAPE_3DP_SESSION): "600.125",  # tie -> 600.12
            ("TSLA", WIRE_SHAPE_3DP_SESSION): "200.134",  # plain -> 200.13
        },
    )
    held, world = _world_for(mini, tmp_path / "scratch")
    assert world.spot_close_quantized_rows == 2
    assert world.spot_close_max_quantization_delta == Decimal("0.005")
    bars = {(b.security_id, b.session): b for b in world.dataset.bars}
    assert bars[("SPY", WIRE_SHAPE_3DP_SESSION)].close == Decimal("600.12")
    assert bars[("TSLA", WIRE_SHAPE_3DP_SESSION)].close == Decimal("200.13")
    block = _census_for(mini, held, world)["spot_close_quantization"]
    assert block["rows_quantized"] == 2
    assert block["max_delta"] == "0.005"


def test_a_trailing_zero_sub_cent_row_is_counted_with_a_zero_delta(tmp_path: Path) -> None:
    """A "200.130" token carries sub-cent EXPONENT but a cent-grid VALUE:
    the boundary rewrites it to "200.13", counts it in custody, and reports
    max_delta 0.000 — the row is disclosed as rewritten-even-though-unmoved,
    never as silently exact."""
    root = tmp_path / "trailing"
    root.mkdir()
    mini = _build_bundle(root, spot_tokens={("TSLA", WIRE_SHAPE_3DP_SESSION): "200.130"})
    held, world = _world_for(mini, tmp_path / "scratch")
    assert world.spot_close_quantized_rows == 1
    assert world.spot_close_max_quantization_delta == Decimal("0.000")
    bar = next(
        b
        for b in world.dataset.bars
        if b.security_id == "TSLA" and b.session == WIRE_SHAPE_3DP_SESSION
    )
    assert (bar.open, bar.high, bar.low, bar.close) == (Decimal("200.13"),) * 4
    block = _census_for(mini, held, world)["spot_close_quantization"]
    assert block["rows_quantized"] == 1
    assert block["max_delta"] == "0.000"


def test_a_stale_mutation_report_fails_criterion_six_against_the_live_registry(
    mini_run,
) -> None:
    """Codex round-1 P0: criterion 6 BINDS the report to the live registry
    at this head. An N/N + restoration report that omits registry mutants
    (a stale report — exactly what M338+ additions would leave behind if the
    full campaign were not re-run) FAILs; so does a report carrying foreign
    ids; and an unsupplied registry never silently certifies the report."""
    mini, run, _replay = mini_run
    trial_payloads = {
        arm: load_json(path)["payload"] for (_lane, arm), path in run.trial_payload_paths.items()
    }
    live = sorted(_live_mutation_registry()[0])
    stale_report = {
        "mutants": [
            {
                "id": mid,
                "file": "src/tree_options/seal/g4_gate.py",
                "invariant": f"registry mutant {mid}",
                "verdict": "KILLED",
            }
            for mid in live[:-1]  # omits exactly one live id
        ],
        "totals": {"KILLED": len(live) - 1},
        "total": len(live) - 1,
        "restoration_suite_passed": True,
    }
    evaluation = _criteria_over(mini, run, trial_payloads, mutation_report=stale_report)
    mutation = evaluation.by_id("mutation_campaign")
    assert mutation.verdict == "FAIL"
    assert any("stale report" in failure for failure in mutation.failures), mutation.failures
    assert evaluation.verdict == "FAIL"

    foreign_report = {
        **stale_report,
        "mutants": [
            *stale_report["mutants"],
            {
                "id": "M999-foreign-id",
                "file": "src/tree_options/seal/g4_gate.py",
                "invariant": "g4 foreign",
                "verdict": "KILLED",
            },
        ],
        "totals": {"KILLED": len(live)},
        "total": len(live),
    }
    evaluation = _criteria_over(mini, run, trial_payloads, mutation_report=foreign_report)
    mutation = evaluation.by_id("mutation_campaign")
    assert mutation.verdict == "FAIL"
    assert any("foreign" in failure for failure in mutation.failures), mutation.failures

    evaluation = _criteria_over(mini, run, trial_payloads, mutation_registry_ids=None)
    mutation = evaluation.by_id("mutation_campaign")
    assert mutation.verdict == "FAIL"
    assert any(
        "live mutation registry was not supplied" in failure for failure in mutation.failures
    ), mutation.failures

    # round-2 P0 probes: the criterion must count KILLED from the ENTRIES
    # (a report whose totals claim N/N while every entry says SURVIVED is a
    # forgery) and reject padded duplicate ids — both probes PASSED before
    live = sorted(_live_mutation_registry()[0])
    digest = _live_mutation_registry()[1]
    forged_verdicts = {
        "mutants": [
            {
                "id": mid,
                "file": "src/tree_options/seal/g4_gate.py",
                "invariant": f"registry mutant {mid}",
                "verdict": "SURVIVED",
            }
            for mid in live
        ],
        "totals": {"KILLED": len(live)},
        "total": len(live),
        "restoration_suite_passed": True,
        "registry_digest": digest,
    }
    evaluation = _criteria_over(mini, run, trial_payloads, mutation_report=forged_verdicts)
    mutation = evaluation.by_id("mutation_campaign")
    assert mutation.verdict == "FAIL"
    assert any("entries say" in failure for failure in mutation.failures), mutation.failures

    padded = {
        **forged_verdicts,
        "mutants": [
            *forged_verdicts["mutants"],
            {**forged_verdicts["mutants"][0], "verdict": "KILLED"},
        ],
        "totals": {"KILLED": len(live) + 1},
        "total": len(live) + 1,
    }
    evaluation = _criteria_over(mini, run, trial_payloads, mutation_report=padded)
    mutation = evaluation.by_id("mutation_campaign")
    assert mutation.verdict == "FAIL"
    assert any("duplicate mutant ids" in f for f in mutation.failures), mutation.failures

    # and a report without the runner's registry digest cannot pose as a
    # campaign run against this registry revision
    undigested = {k: v for k, v in forged_verdicts.items() if k != "registry_digest"}
    undigested["mutants"] = [{**m, "verdict": "KILLED"} for m in undigested["mutants"]]
    evaluation = _criteria_over(mini, run, trial_payloads, mutation_report=undigested)
    mutation = evaluation.by_id("mutation_campaign")
    assert mutation.verdict == "FAIL"
    assert any("registry_digest" in failure for failure in mutation.failures), mutation.failures

    # a WRONG nonempty digest (round-3 P2: not just a missing one) and a
    # report from a DIFFERENT head (round-3 P0: the registry can be
    # identical across commits while the guarded code moved) both FAIL
    wrong_digest = {**forged_verdicts}
    wrong_digest["mutants"] = [{**m, "verdict": "KILLED"} for m in wrong_digest["mutants"]]
    wrong_digest["registry_digest"] = "0" * 64
    evaluation = _criteria_over(mini, run, trial_payloads, mutation_report=wrong_digest)
    mutation = evaluation.by_id("mutation_campaign")
    assert mutation.verdict == "FAIL"
    assert any("does not match the live registry" in failure for failure in mutation.failures), (
        mutation.failures
    )

    other_head = {**wrong_digest, "registry_digest": _live_mutation_registry()[1]}
    evaluation = _criteria_over(mini, run, trial_payloads, mutation_report=other_head)
    mutation = evaluation.by_id("mutation_campaign")
    assert mutation.verdict == "FAIL"
    assert any("is not the sealed head" in f for f in mutation.failures), mutation.failures

    # round-4 P0: the restoration flag is a STRICT boolean — the STRING
    # "false" is truthy under bool() and previously PASSED
    string_false = {**forged_verdicts}
    string_false["mutants"] = [{**m, "verdict": "KILLED"} for m in string_false["mutants"]]
    string_false["head"] = mini.head
    string_false["restoration_suite_passed"] = "false"
    evaluation = _criteria_over(mini, run, trial_payloads, mutation_report=string_false)
    mutation = evaluation.by_id("mutation_campaign")
    assert mutation.verdict == "FAIL"
    assert any("restoration suite did not pass" in f for f in mutation.failures), mutation.failures


def test_the_preflight_rejects_shape_invalid_reports_before_the_event(tmp_path: Path) -> None:
    """Round-4 P0: a PRESENT report whose SHAPE cannot be evaluated (a
    non-int total, a non-boolean restoration flag, a non-list mutants
    array) refuses at preflight — never raises at evaluation time after
    the one-shot has run."""
    from tree_options.seal.g4_gate import (
        MutationReportSchemaError,
        validate_mutation_report,
    )

    for bad in (
        {"total": "not-an-int"},
        {"restoration_suite_passed": "false"},
        {"mutants": "nope"},
        {"totals": {"KILLED": "3"}},
        [1, 2, 3],
    ):
        with pytest.raises(MutationReportSchemaError):
            validate_mutation_report(bad)
    validate_mutation_report(
        {
            "mutants": [{"id": "M1", "verdict": "KILLED"}],
            "totals": {"KILLED": 1},
            "total": 1,
            "restoration_suite_passed": True,
            "head": "a" * 40,
            "registry_digest": "b" * 64,
        }
    )


def test_a_report_that_changes_shape_after_preflight_fails_the_criterion(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Round-4 P0 (TOCTOU residue): preflight validated the report before
    the event; if the file changes shape by evaluation time, criterion 6
    FAILs as a verdict — never an exception after consumption."""
    root = tmp_path / "toctou"
    root.mkdir()
    mini = _build_bundle(root)
    primary_root = tmp_path_factory.mktemp("g4toctou-primary")
    run = _run_mini_gate(
        mini, primary_root / "artifacts", primary_root / "sealed.db", primary_root / "scratch"
    )
    mini.mutation_report.write_text('{"total": "not-an-int"}', encoding="utf-8")
    replay_dir = mini.repo / "artifacts" / "g4-sealed-replay"
    evaluation = evaluate_and_record(
        run,
        verify_sealed_inputs(mini.held_paths),
        paths=_paths_for(mini, run, tmp_path / "evidence", replay_dir),
        repo_root=mini.repo,
        head=mini.head,
    )
    mutation = evaluation.by_id("mutation_campaign")
    assert mutation.verdict == "FAIL"
    assert any("no longer be evaluated" in f for f in mutation.failures), mutation.failures
    assert evaluation.verdict == "FAIL"


def test_the_report_digest_producer_matches_the_gates_recompute() -> None:
    """Round-3 P2: producer/consumer drift — the digest mutate.py STAMPS
    (its own ``registry_digest`` producer, the code the report writer
    calls) must equal the digest the gate RECOMPUTES
    (``live_mutation_registry``) over the same registry file."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mutate_producer", REPO_ROOT / "scripts" / "mutate.py"
    )
    assert spec is not None and spec.loader is not None
    producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)
    assert producer.registry_digest() == _live_mutation_registry()[1]


def test_a_malformed_report_makes_the_runner_preflight_refuse(
    tmp_path: Path,
) -> None:
    """Round-3/5 P0: the runner's preflight() — the method
    execute_sealed_run calls AFTER the authority cross-join and BEFORE the
    durable CONSUMPTION append — refuses an unparseable report (and a
    malformed era census), so nothing is created and nothing is consumed.
    The runner itself does NOT re-preflight inside __call__ (round-5: a
    second check after the append re-opens the consumed-without-verdict
    race); this is the single refusal point, above the spend."""
    from tree_options.seal.g4_gate import GatePreflightError
    from tree_options.seal.runner import RepoCalendarSealedRunner, protocol_calendar_binding

    root = tmp_path / "malformed"
    root.mkdir()
    mini = _build_bundle(root)
    runner = RepoCalendarSealedRunner(protocol_calendar_binding(mini.repo))
    (mini.repo / "artifacts" / "m0-mutations.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(GatePreflightError, match="cannot be parsed"):
        runner.preflight()
    # a malformed ERA census refuses the same way (round-5: existence alone
    # is not evaluability)
    census = mini.repo / "artifacts" / "census" / "43b0b040ea3c" / "census.json"
    census.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(GatePreflightError, match="era census"):
        runner.preflight()
    # and nothing the event would have created exists
    assert not (mini.repo / "artifacts" / "g4-sealed.db").exists()
    assert not (mini.repo / "artifacts" / "g4-sealed").exists()


def test_post_preflight_auxiliary_changes_fail_criteria_never_raise(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Round-6 P0s: auxiliary inputs that change shape AFTER the preflight
    (and so after the CONSUMPTION) become honest criterion FAIL verdicts —
    an unloadable registry (criterion 6), an absent era census (criterion 1)
    and a partial replay dir (criterion 5) never raise post-spend."""
    root = tmp_path / "toctou6"
    root.mkdir()
    mini = _build_bundle(root)
    primary_root = tmp_path_factory.mktemp("g4r6-primary")
    run = _run_mini_gate(
        mini, primary_root / "artifacts", primary_root / "sealed.db", primary_root / "scratch"
    )
    replay_dir = mini.repo / "artifacts" / "g4-sealed-replay"
    if replay_dir.exists():
        shutil.rmtree(replay_dir)
    shutil.copytree(run.artifacts_dir, replay_dir)
    # the REAL flow's shape: the held bundle is verified ONCE (clean tree),
    # and evaluation receives the already-verified object — a post-spend
    # file change never re-trips the dirty-tree guard, exactly like the
    # sealed event
    held = verify_sealed_inputs(mini.held_paths)

    def _evaluate_into(name: str):
        return evaluate_and_record(
            run,
            held,
            paths=_paths_for(mini, run, tmp_path / f"evidence-{name}", replay_dir),
            repo_root=mini.repo,
            head=mini.head,
            # the post-spend shape changes are the SCENARIO (a tracked file
            # edited mid-run dirties the tree); the verdict must still be
            # recorded — the stamping discipline's dirty refusal is a
            # separate, pre-spend concern (execute's re-verify)
            allow_dirty=True,
        )

    # (a) the registry becomes UNLOADABLE-but-present after the preflight
    # (a deleted file returns None cleanly; a syntax-broken one raises —
    # the case the exception-safe derive exists for)
    registry = mini.repo / "scripts" / "mutate.py"
    saved_registry = registry.read_bytes()
    registry.write_text("def broken(:\n", encoding="utf-8")
    try:
        evaluation = _evaluate_into("registry")
        assert evaluation.by_id("mutation_campaign").verdict == "FAIL"
        assert any(
            "registry was not supplied" in f for f in evaluation.by_id("mutation_campaign").failures
        )
    finally:
        registry.write_bytes(saved_registry)

    # (a2, round-7 P0) a SYNTACTICALLY LOADABLE registry whose MUTANTS
    # entries are malformed (no "id") — the extraction sits inside the
    # wrapped boundary, so this is a verdict, never a raw KeyError
    registry.write_text("MUTANTS = [{}]\n", encoding="utf-8")
    try:
        evaluation = _evaluate_into("registry-malformed-entries")
        assert evaluation.by_id("mutation_campaign").verdict == "FAIL"
    finally:
        registry.write_bytes(saved_registry)

    # (b) the era census is REMOVED after the preflight (the copy the gate
    # paths actually read: mini.era_census, outside the fixture repo)
    saved_census = mini.era_census.read_bytes()
    mini.era_census.unlink()
    try:
        evaluation = _evaluate_into("census-absent")
        first = evaluation.by_id("manifest_integrity")
        assert first.verdict == "FAIL"
        assert any("absent" in f for f in first.failures), first.failures
    finally:
        mini.era_census.write_bytes(saved_census)

    # (c) a present but PARTIAL replay dir (one payload removed)
    payloads = sorted(p for p in replay_dir.rglob("*.json") if p.name != "sealed-gate-summary.json")
    removed = payloads[0]
    saved_payload = removed.read_bytes()
    removed.unlink()
    try:
        evaluation = _evaluate_into("replay-partial")
        assert evaluation.by_id("determinism").verdict == "FAIL"
    finally:
        removed.write_bytes(saved_payload)

    # (d, round-9 P2) deeply nested JSON swapped in post-preflight — the
    # EVALUATION handlers convert it to a verdict, never a raw
    # RecursionError after consumption
    deep = "[" * 100_000 + "]" * 100_000
    saved_report = mini.mutation_report.read_bytes()
    mini.mutation_report.write_text(deep, encoding="utf-8")
    try:
        evaluation = _evaluate_into("deep-report")
        assert evaluation.by_id("mutation_campaign").verdict == "FAIL"
    finally:
        mini.mutation_report.write_bytes(saved_report)
    saved_census = mini.era_census.read_bytes()
    mini.era_census.write_text(deep, encoding="utf-8")
    try:
        evaluation = _evaluate_into("deep-census")
        assert evaluation.by_id("manifest_integrity").verdict == "FAIL"
    finally:
        mini.era_census.write_bytes(saved_census)

    # and the quiet control: with everything restored, the verdict is PASS
    evaluation = _evaluate_into("restored")
    assert evaluation.verdict == "PASS"


def test_an_aliased_replay_cannot_certify_determinism_by_self_comparison(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Round-8 P0: a "replay" whose payloads are SYMLINKS onto the run's own
    artifacts compares byte-identical BY CONSTRUCTION — criterion 5 must
    name the aliasing and FAIL, never certify determinism by
    self-comparison (Codex's four-symlink probe returned PASS)."""
    root = tmp_path / "aliased"
    root.mkdir()
    mini = _build_bundle(root)
    primary_root = tmp_path_factory.mktemp("g4alias-primary")
    run = _run_mini_gate(
        mini, primary_root / "artifacts", primary_root / "sealed.db", primary_root / "scratch"
    )
    held = verify_sealed_inputs(mini.held_paths)
    replay_dir = mini.repo / "artifacts" / "g4-sealed-replay"
    if replay_dir.exists():
        shutil.rmtree(replay_dir)
    replay_dir.mkdir(parents=True)
    for payload in sorted(run.artifacts_dir.rglob("*.json")):
        if payload.name == "sealed-gate-summary.json":
            continue
        target = replay_dir / payload.name
        target.symlink_to(payload)
    evaluation = evaluate_and_record(
        run,
        held,
        paths=_paths_for(mini, run, tmp_path / "evidence", replay_dir),
        repo_root=mini.repo,
        head=mini.head,
    )
    determinism = evaluation.by_id("determinism")
    assert determinism.verdict == "FAIL"
    assert any("not independent" in f for f in determinism.failures), determinism.failures
    assert evaluation.verdict == "FAIL"

    # round-9 P0: HARD LINKS carry no symlink bit yet share the run's own
    # inodes — is_symlink alone missed them and the probe PASSED; the
    # (st_dev, st_ino) comparison names the aliasing
    shutil.rmtree(replay_dir)
    replay_dir.mkdir(parents=True)
    for payload in sorted(run.artifacts_dir.rglob("*.json")):
        if payload.name == "sealed-gate-summary.json":
            continue
        os.link(payload, replay_dir / payload.name)
    evaluation = evaluate_and_record(
        run,
        held,
        paths=_paths_for(mini, run, tmp_path / "evidence-hardlink", replay_dir),
        repo_root=mini.repo,
        head=mini.head,
    )
    determinism = evaluation.by_id("determinism")
    assert determinism.verdict == "FAIL"
    assert any("not independent" in f for f in determinism.failures), determinism.failures


def test_a_replay_payload_vanishing_mid_check_never_raises(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-10 P0 (the final round's own finding): the alias check's stat
    can hit a payload that vanishes mid-check — exists-then-stat is a race.
    The OSError is treated as absence (criterion 5's own missing-payload
    failure), never a raw FileNotFoundError after consumption. Simulated by
    making Path.stat raise for replay payloads, as the race would."""
    root = tmp_path / "vanish"
    root.mkdir()
    mini = _build_bundle(root)
    primary_root = tmp_path_factory.mktemp("g4vanish-primary")
    run = _run_mini_gate(
        mini, primary_root / "artifacts", primary_root / "sealed.db", primary_root / "scratch"
    )
    held = verify_sealed_inputs(mini.held_paths)
    replay_dir = mini.repo / "artifacts" / "g4-sealed-replay"
    if replay_dir.exists():
        shutil.rmtree(replay_dir)
    shutil.copytree(run.artifacts_dir, replay_dir)
    real_stat = Path.stat
    real_read_bytes = Path.read_bytes

    def racing_stat(self: Path, *args: object, **kwargs: object) -> object:
        if self.parent == replay_dir:
            raise FileNotFoundError(str(self))
        return real_stat(self, *args, **kwargs)  # type: ignore[arg-type,return-value]

    def racing_read_bytes(self: Path) -> bytes:
        if self.parent == replay_dir:
            raise FileNotFoundError(str(self))
        return real_read_bytes(self)  # type: ignore[return-value]

    monkeypatch.setattr(Path, "stat", racing_stat)
    monkeypatch.setattr(Path, "read_bytes", racing_read_bytes)
    evaluation = evaluate_and_record(
        run,
        held,
        paths=_paths_for(mini, run, tmp_path / "evidence", replay_dir),
        repo_root=mini.repo,
        head=mini.head,
    )
    monkeypatch.undo()
    determinism = evaluation.by_id("determinism")
    assert determinism.verdict == "FAIL"
    assert evaluation.verdict == "FAIL"


def test_deeply_nested_auxiliary_json_never_raises_post_consumption(
    tmp_path: Path,
) -> None:
    """Round-8 P0: json.loads on a deeply nested document raises
    RecursionError (a RuntimeError the ValueError handlers cannot contain).
    The report and the census both refuse at preflight and FAIL as verdicts
    at evaluation — never a raw escape."""
    from tree_options.seal.g4_gate import GatePreflightError
    from tree_options.seal.runner import RepoCalendarSealedRunner, protocol_calendar_binding

    deep = "[" * 100_000 + "]" * 100_000
    root = tmp_path / "deep"
    root.mkdir()
    mini = _build_bundle(root)
    runner = RepoCalendarSealedRunner(protocol_calendar_binding(mini.repo))
    (mini.repo / "artifacts" / "m0-mutations.json").write_text(deep, encoding="utf-8")
    with pytest.raises(GatePreflightError, match="cannot be parsed"):
        runner.preflight()
    (mini.repo / "artifacts" / "census" / "43b0b040ea3c" / "census.json").write_text(
        deep, encoding="utf-8"
    )
    with pytest.raises(GatePreflightError, match="cannot be parsed"):
        runner.preflight()


def test_an_era_census_with_non_integer_counts_refuses_at_preflight(
    tmp_path: Path,
) -> None:
    """Round-6/7 P0/P2: a JSON float count (1e309 parses to inf) passes a
    plain shape check and raises OverflowError at int() only after the
    one-shot ran, and a JSON `true` count SUBCLASSES int (True == 1) and
    would certify a count that was never stamped as a number —
    era_target_of requires TRUE ints, bools included, and the preflight
    calls it."""
    from tree_options.seal.g4_gate import GatePreflightError
    from tree_options.seal.runner import RepoCalendarSealedRunner, protocol_calendar_binding

    root = tmp_path / "bad-census"
    root.mkdir()
    mini = _build_bundle(root)
    census = mini.repo / "artifacts" / "census" / "43b0b040ea3c" / "census.json"
    runner = RepoCalendarSealedRunner(protocol_calendar_binding(mini.repo))
    for bad_count in ("1e309", "true", "2.0", '"2"'):
        census.write_text(
            json.dumps(
                {
                    "coverage": {"expected_masters": json.loads(bad_count)},
                    "values": {"observed_census_fact": {"distinct_contracts": {"v": 6}}},
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(GatePreflightError, match="cannot be evaluated"):
            runner.preflight()


def test_a_sub_cent_positive_close_refuses_naming_the_row(tmp_path: Path) -> None:
    """The fail-closed guard: a positive close under one cent ("0.005")
    quantizes to 0.00, which Price (gt=0) can never carry — the build
    REFUSES, naming the underlying, the session, and the original token,
    rather than silently flooring the row to zero."""
    root = tmp_path / "subcent"
    root.mkdir()
    mini = _build_bundle(
        root, spot_tokens={(WIRE_SHAPE_3DP_UNDERLYING, WIRE_SHAPE_3DP_SESSION): "0.005"}
    )
    with pytest.raises(ValueError, match=r"0\.005 for SPY on 2024-06-14") as excinfo:
        _world_for(mini, tmp_path / "scratch")
    assert "quantizes to 0.00" in str(excinfo.value)
    assert "refusing" in str(excinfo.value).lower()


# ---- Fix B: the six pre-declared criteria + the verbatim verdict -------------------


def test_the_mini_gate_passes_and_records_the_verdict_verbatim(mini_run, tmp_path: Path) -> None:
    """The PASS shape: all six criteria PASS on the fixture world, the
    evidence triple is written, and the verdict is recorded verbatim."""
    evaluation, _replay_dir = _evaluate(mini_run, tmp_path / "evidence")
    assert evaluation.verdict == "PASS"
    assert [o.verdict for o in evaluation.criteria] == ["PASS"] * 6
    assert [o.criterion_id for o in evaluation.criteria] == [
        "manifest_integrity",
        "candidate_discipline",
        "fill_discipline",
        "rejection_paths_live",
        "determinism",
        "mutation_campaign",
    ]
    assert (tmp_path / "evidence" / "m4-g4-sealed-gate.json").is_file()
    assert (tmp_path / "evidence" / "m4-g4-sealed-gate.md").is_file()
    log = (tmp_path / "evidence" / "m4-g4-sealed-gate.log").read_text(encoding="utf-8")
    assert "SEALED_GATE_VERDICT=PASS" in log
    assert log.count("SEALED_CHECK PASS") == 6
    recorded = json.loads(
        (tmp_path / "evidence" / "m4-g4-sealed-gate.json").read_text(encoding="utf-8")
    )
    assert recorded["verdict"] == "PASS"
    assert recorded["head"] == mini_run[0].head
    fill = evaluation.by_id("fill_discipline")
    assert fill.reported["n_fills"] > 0
    assert fill.reported["over_participation_pairs"] == 0


def test_the_clean_clone_replay_reproduces_the_payload_hashes(mini_run) -> None:
    """Criterion 5's substance: two independent runs over the same held
    bytes produce byte-identical stamped payloads."""
    import hashlib

    _mini, run, replay = mini_run

    def hashes(artifacts: Path) -> dict[str, str]:
        return {
            str(p.relative_to(artifacts)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(artifacts.rglob("*.json"))
            if p.name != "sealed-gate-summary.json"  # the verdict record, not a payload
        }

    assert hashes(run.artifacts_dir) == hashes(replay.artifacts_dir)


def test_a_discipline_violation_in_a_stamped_payload_fails_criterion_and_verdict(
    mini_run, tmp_path: Path
) -> None:
    """M333's owner: inject a fill-discipline violation into a stamped
    payload — that criterion FAILs, the verdict FAILs, and the failure is
    recorded verbatim."""
    mini, run, _replay = mini_run
    arm_a = run.trial_payload_paths[("2", "A")]
    body = json.loads(arm_a.read_text(encoding="utf-8"))
    victim = body["payload"]["fills_log"][0]
    victim["quantity"] = victim["bar_volume"] + 5  # over-participation
    victim["bar_session"] = "2023-01-06"  # far more than one session back
    trial_payloads = {
        arm: (body["payload"] if arm == "A" else load_json(path)["payload"])
        for (_lane, arm), path in run.trial_payload_paths.items()
    }
    evaluation = _criteria_over(mini, run, trial_payloads)
    assert evaluation.verdict == "FAIL"
    fill = evaluation.by_id("fill_discipline")
    assert fill.verdict == "FAIL"
    assert any("not exactly 1" in failure for failure in fill.failures), fill.failures
    assert any("participation cap exceeded" in f for f in fill.failures), fill.failures


def _criteria_over(
    mini,
    run,
    trial_payloads,
    *,
    lane2_census=None,
    floor: int = MINI_FLOOR,
    lane1_floor: int = MINI_FLOOR,
    era_contracts: int = 6,
    mutation_report=None,
    mutation_registry_ids="unset",
    mutation_registry_digest="unset",
    head="unset",
):
    from tree_options.protocol.loader import load_protocol_bytes

    live = _live_mutation_registry()
    held = verify_sealed_inputs(mini.held_paths)
    return evaluate_g4_criteria(
        protocol=load_protocol_bytes(held.protocol_bytes),
        lane1_census=load_json(run.census_payload_paths["lane1"])["payload"],
        lane2_census=(
            lane2_census
            if lane2_census is not None
            else load_json(run.census_payload_paths["lane2"])["payload"]
        ),
        trial_payloads=trial_payloads,
        trial_statuses={"lane2|A": "COMPLETED", "lane2|B": "COMPLETED"},
        execution_calendar=run.execution_calendar,
        stamped_hashes={},
        replay_hashes={},
        mutation_report=(
            load_json(mini.mutation_report) if mutation_report is None else mutation_report
        ),
        mutation_registry_ids=(
            live[0] if mutation_registry_ids == "unset" else mutation_registry_ids
        ),
        mutation_registry_digest=(
            live[1] if mutation_registry_digest == "unset" else mutation_registry_digest
        ),
        head=(mini.head if head == "unset" else head),
        era_target={"expected_masters": 2, "distinct_contracts": era_contracts},
        rejection_floor=floor,
        rejection_lane1_floor=lane1_floor,
    )


def test_over_participation_fails_the_fill_discipline_criterion(mini_run) -> None:
    """M335's owner: a stamped fill sequence whose cumulative participation
    per (contract, bar session) exceeds the bar's observed volume FAILS
    criterion 3 by name."""
    mini, run, _replay = mini_run
    arm_a = run.trial_payload_paths[("2", "A")]
    body = json.loads(arm_a.read_text(encoding="utf-8"))
    payload = body["payload"]
    assert payload["fills_log"], "the fixture world must actually fill"
    victim = payload["fills_log"][0]
    victim["quantity"] = victim["bar_volume"] + 1  # one contract over the bar
    evaluation = _criteria_over(mini, run, {"A": payload, "B": payload})
    fill = evaluation.by_id("fill_discipline")
    assert fill.verdict == "FAIL"
    assert any("participation cap exceeded" in f for f in fill.failures), fill.failures
    assert fill.reported["over_participation_pairs"] == 1


def test_the_strict_lane2_class_map_never_counts_no_bar(mini_run) -> None:
    """M334's owner: a lane-2 census whose COUNTED classes sit below the
    floor but whose disclosed no_bar rows are plentiful must FAIL — counting
    the availability disclosure would inflate the lane to the floor."""
    mini, run, _replay = mini_run
    lane2 = load_json(run.census_payload_paths["lane2"])["payload"]
    lane2 = {
        **lane2,
        "rejection_classes": {
            "zero_volume_bar_refusals": 1,
            "massive_derivation_error_refusals": 1,
            "master_row_refusals": 0,
            "no_bar_not_evaluable_disclosed": 100,
        },
    }
    # strip the trial histogram's flow-FAIL rows too, so the ONLY thing that
    # could inflate the lane to the floor is the disclosed no_bar count
    trial_payloads = {}
    for (_lane, arm), path in run.trial_payload_paths.items():
        payload = dict(load_json(path)["payload"])
        counters = dict(payload["counters"])
        histogram = {
            rule: {
                status: n
                for status, n in rows.items()
                if not (rule == "session_volume_flow" and status == "FAIL")
            }
            for rule, rows in counters["rule_histogram"].items()
        }
        counters["rule_histogram"] = histogram
        payload["counters"] = counters
        trial_payloads[arm] = payload
    evaluation = _criteria_over(mini, run, trial_payloads, lane2_census=lane2)
    rejection = evaluation.by_id("rejection_paths_live")
    assert rejection.verdict == "FAIL"
    assert any("pooled counted rejections 2 < 3" in f for f in rejection.failures)
    assert rejection.reported["lane2"]["no_bar_not_evaluable_disclosed"] == 100


def test_a_flow_threshold_drift_fails_candidate_discipline(mini_run) -> None:
    """Criterion 2's pin: the stamped threshold must equal the 0.2.1
    amendment value EXACTLY — any drift is a FAIL."""
    mini, run, _replay = mini_run
    trial_payloads = {
        arm: load_json(path)["payload"] for (_l, arm), path in run.trial_payload_paths.items()
    }
    trial_payloads["A"] = {**trial_payloads["A"], "flow_min_session_volume": 101}
    evaluation = _criteria_over(mini, run, trial_payloads)
    candidate = evaluation.by_id("candidate_discipline")
    assert candidate.verdict == "FAIL"
    assert any("101" in failure for failure in candidate.failures)


def test_criterion1_is_the_custody_identity(mini_run) -> None:
    """Owner ruling 2026-09-01 (post-FAIL remediation): the era census stamps
    the MASTERS domain; the manifest verifies the OVERLAY-ACCEPTED domain.
    Refused master rows are counted custody — the criterion passes when
    verified + refused == stamped, and a real gap (a row neither verified
    nor refused-counted) is the failure, never the honest refusal. The
    fixture world: 5 verified + 1 refused master row == the stamped 6."""
    mini, run, _replay = mini_run
    lane2 = load_json(run.census_payload_paths["lane2"])["payload"]
    verified = int(lane2["manifest"]["verified_series"])
    refused = int(lane2["rejection_classes"]["master_row_refusals"])
    assert refused >= 1, "the fixture must carry the refused-master class"
    trial_payloads = {
        arm: load_json(path)["payload"] for (_l, arm), path in run.trial_payload_paths.items()
    }
    # the masters-domain stamp: verified + refused (the honest census target)
    evaluation = _criteria_over(mini, run, trial_payloads, era_contracts=verified + refused)
    manifest = evaluation.by_id("manifest_integrity")
    assert manifest.verdict == "PASS", manifest.failures
    assert manifest.reported["lane2"]["master_row_refusals"] == refused
    # a REAL gap — a stamp the custody cannot account for — fails naming the
    # identity (this is the silent-loss case the criterion exists to catch)
    gap = _criteria_over(mini, run, trial_payloads, era_contracts=verified + refused + 1)
    assert gap.by_id("manifest_integrity").verdict == "FAIL"
    assert any(
        "custody" in f and f"distinct_contracts {verified + refused + 1}" in f
        for f in gap.by_id("manifest_integrity").failures
    )


def test_the_real_lane1_floor_is_zero(mini_run) -> None:
    """Owner ruling 2026-09-01 (post-FAIL remediation): the pre-declared
    lane-1 floor of 50 FIRING parse refusals was the pending pre-run
    calibration, and its premise measured false — the REAL retained Cboe
    session parses perfectly clean (0 firing refusals; the 723 zero-bid
    rows are the disclosed audit statistic). The REAL lane-1 floor is 0;
    fixture gates keep an explicit floor for teeth (the lane-2 floor stays
    pre-declared at 50 for the real run)."""
    mini, run, _replay = mini_run
    trial_payloads = {
        arm: load_json(path)["payload"] for (_l, arm), path in run.trial_payload_paths.items()
    }
    clean_lane1 = {
        **load_json(run.census_payload_paths["lane1"])["payload"],
        "rejection_classes": {"firing_parse_refusals": 0, "zero_bid_rows_disclosed": 723},
    }
    from tree_options.protocol.loader import load_protocol_bytes

    held = verify_sealed_inputs(mini.held_paths)
    base = dict(
        protocol=load_protocol_bytes(held.protocol_bytes),
        lane1_census=clean_lane1,
        lane2_census=load_json(run.census_payload_paths["lane2"])["payload"],
        trial_payloads=trial_payloads,
        trial_statuses={"lane2|A": "COMPLETED", "lane2|B": "COMPLETED"},
        execution_calendar=run.execution_calendar,
        stamped_hashes={},
        replay_hashes={},
        mutation_report=load_json(mini.mutation_report),
    )
    live = _live_mutation_registry()
    from tree_options.seal.g4_gate import REJECTION_LANE1_FLOOR

    assert REJECTION_LANE1_FLOOR == 0, "the real lane-1 floor is the 0 ruling"
    real = evaluate_g4_criteria(
        **base,
        mutation_registry_ids=live[0],
        mutation_registry_digest=live[1],
        head=mini.head,
        era_target={"expected_masters": 2, "distinct_contracts": 6},
        rejection_floor=MINI_FLOOR,
    )  # lane1 floor defaults to the ruling's 0
    assert real.by_id("rejection_paths_live").verdict == "PASS"
    assert real.by_id("rejection_paths_live").reported["lane1"]["floor"] == 0
    # an EXPLICIT lane-1 floor still has teeth (the fixture gates' form)
    fixture = evaluate_g4_criteria(
        **base,
        mutation_registry_ids=live[0],
        mutation_registry_digest=live[1],
        head=mini.head,
        era_target={"expected_masters": 2, "distinct_contracts": 6},
        rejection_floor=MINI_FLOOR,
        rejection_lane1_floor=5,
    )
    rejection = fixture.by_id("rejection_paths_live")
    assert rejection.verdict == "FAIL"
    assert any("lane 1: pooled FIRING parse refusals 0 < 5" in f for f in rejection.failures)


def test_a_manifest_count_mismatch_fails_manifest_integrity(mini_run) -> None:
    mini, run, _replay = mini_run
    trial_payloads = {
        arm: load_json(path)["payload"] for (_l, arm), path in run.trial_payload_paths.items()
    }
    from tree_options.protocol.loader import load_protocol_bytes

    held = verify_sealed_inputs(mini.held_paths)
    mismatch = evaluate_g4_criteria(
        protocol=load_protocol_bytes(held.protocol_bytes),
        lane1_census=load_json(run.census_payload_paths["lane1"])["payload"],
        lane2_census=load_json(run.census_payload_paths["lane2"])["payload"],
        trial_payloads=trial_payloads,
        trial_statuses={"lane2|A": "COMPLETED", "lane2|B": "COMPLETED"},
        execution_calendar=run.execution_calendar,
        stamped_hashes={},
        replay_hashes={},
        mutation_report=load_json(mini.mutation_report),
        era_target={"expected_masters": 2, "distinct_contracts": 99},
        rejection_floor=MINI_FLOOR,
    )
    manifest = mismatch.by_id("manifest_integrity")
    assert manifest.verdict == "FAIL"
    assert any("distinct_contracts 99" in f for f in manifest.failures)


def test_a_missing_replay_or_mutation_report_fails_honestly(mini_run) -> None:
    """Criteria 5/6 with absent auxiliary inputs are FAILURES, never silent
    skips; and the mutation report must be N/N with verdict-logic coverage."""
    mini, run, _replay = mini_run
    trial_payloads = {
        arm: load_json(path)["payload"] for (_l, arm), path in run.trial_payload_paths.items()
    }
    from tree_options.protocol.loader import load_protocol_bytes

    held = verify_sealed_inputs(mini.held_paths)
    common = dict(
        protocol=load_protocol_bytes(held.protocol_bytes),
        lane1_census=load_json(run.census_payload_paths["lane1"])["payload"],
        lane2_census=load_json(run.census_payload_paths["lane2"])["payload"],
        trial_payloads=trial_payloads,
        trial_statuses={"lane2|A": "COMPLETED", "lane2|B": "COMPLETED"},
        execution_calendar=run.execution_calendar,
        stamped_hashes={},
        era_target={"expected_masters": 2, "distinct_contracts": 5},
        rejection_floor=MINI_FLOOR,
    )
    none_supplied = evaluate_g4_criteria(replay_hashes=None, mutation_report=None, **common)
    assert none_supplied.by_id("determinism").verdict == "FAIL"
    assert none_supplied.by_id("mutation_campaign").verdict == "FAIL"
    partial = dict(load_json(mini.mutation_report))
    partial["total"] = 2
    partial["mutants"] = [
        {"id": "M01-unrelated", "file": "src/tree_options/guards/availability.py"}
    ]
    partial_report = evaluate_g4_criteria(replay_hashes={}, mutation_report=partial, **common)
    campaign = partial_report.by_id("mutation_campaign")
    assert campaign.verdict == "FAIL"
    assert any("not N/N" in f for f in campaign.failures)
    assert any("verdict logic" in f for f in campaign.failures)


def test_lane1_inapplicability_is_declared_never_silent(mini_run, tmp_path: Path) -> None:
    evaluation, _replay_dir = _evaluate(mini_run, tmp_path / "evidence")
    candidate = evaluation.by_id("candidate_discipline")
    fill = evaluation.by_id("fill_discipline")
    assert candidate.lane1_applicability == "declared_inapplicable"
    assert "T+1 publication wall" in candidate.lane1_inapplicable_reason
    assert fill.lane1_applicability == "declared_inapplicable"
    assert "one retained session forbids an execution session" in (fill.lane1_inapplicable_reason)
    recorded = json.loads(
        (tmp_path / "evidence" / "m4-g4-sealed-gate.json").read_text(encoding="utf-8")
    )
    by_id = {c["id"]: c for c in recorded["criteria"]}
    assert by_id["fill_discipline"]["lane1_applicability"] == "declared_inapplicable"
    assert by_id["fill_discipline"]["lane1_inapplicable_reason"]


def test_the_gate_cli_requires_yes_and_then_records_the_verdict(
    mini_run, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fix B's CLI end to end: without --yes nothing is read; with --yes the
    held packet's input paths are verified, the trials run ONCE, the verdict
    is printed + recorded, and the exit code follows it."""
    import importlib.util
    from pathlib import Path as _Path

    spec = importlib.util.spec_from_file_location(
        "run_m4_sealed_gate",
        _Path(__file__).resolve().parents[2] / "scripts" / "run_m4_sealed_gate.py",
    )
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    mini, _run, replay_run = mini_run
    args = [
        "--repo",
        str(mini.repo),
        "--lane1-manifest",
        str(mini.held_paths.lane1_manifest),
        "--lane1-source",
        str(mini.held_paths.lane1_source),
        "--lane2-manifest",
        str(mini.held_paths.lane2_manifest),
        "--calendar-decision-artifact",
        str(mini.held_paths.calendar_decision_artifact),
        "--evidence-root",
        str(tmp_path / "evidence"),
        "--registry",
        str(tmp_path / "sealed.db"),
        "--artifacts-dir",
        str(tmp_path / "artifacts"),
        "--scratch-root",
        str(tmp_path / "scratch"),
        "--era-census",
        str(mini.era_census),
        "--replay-artifacts",
        str(replay_run.artifacts_dir),
        "--mutation-report",
        str(mini.mutation_report),
        "--spot-proxy-v2",
        str(mini.spot_v2),
    ]
    # without --yes: refused, nothing read
    assert cli.run_gate([*args]) == 2
    assert "REFUSED: --yes is required" in capsys.readouterr().err
    # with --yes: the one-shot run + verdict, at the DEFAULT Agenda-D geometry
    assert cli.run_gate([*args, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "SEALED_GATE_VERDICT=PASS" in out
    assert out.count("SEALED_CHECK PASS") == 6
    assert (tmp_path / "evidence" / "m4-g4-sealed-gate.json").is_file()
    # a second invocation refuses (one-shot)
    with pytest.raises(RuntimeError, match=r"refusing to reuse sealed (registry|artifacts)"):
        cli.run_gate([*args, "--yes"])


# ---- Fix C: the runner is the machinery -------------------------------------------


def test_the_production_runner_delegates_to_the_machinery(
    mini_gate: MiniGateFixture, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The runner consumes the held bundle and returns the outcome string
    (run id + verdict + evidence paths) under the PRODUCTION path set —
    RUN-SCOPED to this sealed run's id (the successor-enablement lane: the
    outputs land under artifacts/g4-sealed-runs/<sealed_run_id>/, never at
    the crashed 2026-08-31 event's occupied legacy names) — and the DEFAULT
    geometry; a second invocation refuses (one-shot, inside the run root)."""
    from tree_options.seal.identity import sealed_run_id
    from tree_options.seal.verified_inputs import identity_from_packet

    replay_root = tmp_path_factory.mktemp("g4runner-replay")
    replay_run = _run_mini_gate(
        mini_gate,
        replay_root / "artifacts",
        replay_root / "sealed.db",
        replay_root / "scratch",
    )
    held = verify_sealed_inputs(mini_gate.held_paths)
    run_id = sealed_run_id(identity_from_packet(held.packet))
    run_root = mini_gate.repo / "artifacts" / "g4-sealed-runs" / run_id
    replay_dir = run_root / "replay"
    if replay_dir.exists():
        shutil.rmtree(replay_dir)
    shutil.copytree(replay_run.artifacts_dir, replay_dir)

    runner = RepoCalendarSealedRunner(protocol_calendar_binding(mini_gate.repo))
    assert runner.config_digest() == runner.config_digest()  # deterministic
    outcome = runner(held)
    assert outcome.startswith("m4-g4-sealed/1 sealed_run_id=")
    assert "verdict=PASS" in outcome
    assert "m4-g4-sealed-gate.json" in outcome
    # the run-scoped workspace: registry, artifacts and the replay the
    # determinism criterion compared all live under the run root, and the
    # LEGACY names stay untouched (the crashed run's residue is history)
    assert (run_root / "g4-sealed.db").is_file()
    assert (run_root / "artifacts" / "sealed-gate-summary.json").is_file()
    assert (run_root / "replay").is_dir()
    assert not (mini_gate.repo / "artifacts" / "g4-sealed.db").exists()
    assert not (mini_gate.repo / "artifacts" / "g4-sealed").exists()
    evidence = mini_gate.repo / "docs" / "evidence-logs" / "m4"
    assert (evidence / "m4-g4-sealed-gate.json").is_file()
    recorded = json.loads((evidence / "m4-g4-sealed-gate.json").read_text(encoding="utf-8"))
    assert recorded["verdict"] == "PASS"
    with pytest.raises(
        RuntimeError, match=r"refusing to reuse sealed (registry|artifacts|scratch)"
    ):
        runner(held)


def test_the_predeclared_floor_default_is_50() -> None:
    """The sealed default is the pre-declared pooled floor; the mini gates'
    smaller floor is a test-only parameter, never a machinery default."""
    assert REJECTION_FLOOR == 50
