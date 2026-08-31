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


def _build_fixture_repo(root: Path) -> Path:
    repo = root / "repo"
    for relative in (
        Path(".gitignore"),
        Path("research_protocol.yaml"),
        Path("data/g4/sealed-criteria.json"),
        Path("docs/m4-g4-sealed-gate-plan.md"),
        Path("data/calendar/nyse_sessions_2018_01_02_2026_12_31.json"),
        Path("data/calendar/nyse_sessions_2018_01_02_2026_12_31.sha256"),
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


def _build_lane2(root: Path) -> tuple[Path, Path]:
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
        "values": {"observed_census_fact": {"distinct_contracts": {"v": 5}}},
    }
    path = root / "census.json"
    path.write_text(json.dumps(census), encoding="utf-8")
    return path


def _build_mutation_report(root: Path) -> Path:
    report = {
        "mutants": [
            {
                "id": "M333-g4-verdict-and-to-or",
                "file": "src/tree_options/seal/g4_gate.py",
                "invariant": "g4 the gate verdict is FAIL when any criterion fails",
                "verdict": "KILLED",
            }
        ],
        "totals": {"KILLED": 1},
        "total": 1,
        "restoration_suite_passed": True,
    }
    path = root / "m0-mutations.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def mini_gate(tmp_path_factory: pytest.TempPathFactory) -> MiniGateFixture:
    """The full fixture bundle: a committed fixture repo (a REAL git repo so
    the machinery's fail-closed stamping runs the sealed discipline), both
    lanes' synthetic captures, and the auxiliary stamped inputs."""
    root = tmp_path_factory.mktemp("g4mach")
    repo = _build_fixture_repo(root)
    source, lane1_manifest = _build_lane1(root)
    lane2_manifest, spot_v2 = _build_lane2(root)
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
    mutation_report = _build_mutation_report(root)
    # the PRODUCTION path set the runner consumes (production_gate_paths)
    production_census = repo / "artifacts" / "census" / "43b0b040ea3c" / "census.json"
    production_census.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(era_census, production_census)
    production_report = repo / "artifacts" / "m0-mutations.json"
    shutil.copyfile(mutation_report, production_report)
    production_v2 = repo / "artifacts" / "spot-proxy-v2.json"
    shutil.copyfile(spot_v2, production_v2)
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
    return MiniGateFixture(
        repo=repo,
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
            head="0" * 40,
            rejection_floor=floor,
        ),
        replay_dir,
    )


# ---- Fix A: the lane-2 null-trial library seam ------------------------------------


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
    assert recorded["head"] == "0" * 40
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


def _criteria_over(mini, run, trial_payloads, *, lane2_census=None, floor: int = MINI_FLOOR):
    from tree_options.protocol.loader import load_protocol_bytes

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
        mutation_report=load_json(mini.mutation_report),
        era_target={"expected_masters": 2, "distinct_contracts": 5},
        rejection_floor=floor,
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
    (run id + verdict + evidence paths) under the PRODUCTION path set and
    the DEFAULT geometry; a second invocation refuses (one-shot)."""
    replay_root = tmp_path_factory.mktemp("g4runner-replay")
    replay_run = _run_mini_gate(
        mini_gate,
        replay_root / "artifacts",
        replay_root / "sealed.db",
        replay_root / "scratch",
    )
    replay_dir = mini_gate.repo / "artifacts" / "g4-sealed-replay"
    if replay_dir.exists():
        shutil.rmtree(replay_dir)
    shutil.copytree(replay_run.artifacts_dir, replay_dir)

    held = verify_sealed_inputs(mini_gate.held_paths)
    runner = RepoCalendarSealedRunner(protocol_calendar_binding(mini_gate.repo))
    assert runner.config_digest() == runner.config_digest()  # deterministic
    outcome = runner(held)
    assert outcome.startswith("m4-g4-sealed/1 sealed_run_id=")
    assert "verdict=PASS" in outcome
    assert "m4-g4-sealed-gate.json" in outcome
    evidence = mini_gate.repo / "docs" / "evidence-logs" / "m4"
    assert (evidence / "m4-g4-sealed-gate.json").is_file()
    recorded = json.loads((evidence / "m4-g4-sealed-gate.json").read_text(encoding="utf-8"))
    assert recorded["verdict"] == "PASS"
    with pytest.raises(RuntimeError, match=r"refusing to reuse sealed (registry|artifacts)"):
        runner(held)


def test_the_predeclared_floor_default_is_50() -> None:
    """The sealed default is the pre-declared pooled floor; the mini gates'
    smaller floor is a test-only parameter, never a machinery default."""
    assert REJECTION_FLOOR == 50
