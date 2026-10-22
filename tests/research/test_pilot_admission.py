"""Wave 4 lane I: pilot budgets are enforced at job admission (C3.5).

A research run that carries ``pilot_program_id`` passes the pilot admission
in ``ResearchWorker._process`` before it is claimed: a draft never runs, an
exhausted or unknown budget refuses it before any compute, and an admitted
job is metered (one request at admission, its compute seconds and published
result bytes when it ends). Runs without the field are untouched.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tree_options.research.comparison.engine import run_comparison
from tree_options.research.contracts import (
    BorrowingPolicy,
    CashflowTiming,
    CollateralPolicy,
    ComparisonSpec,
    CostModelKind,
    Currency,
    FundedHistorySupport,
    IdleCashPolicy,
    PositionSizing,
    PriceBasis,
    Rebalancing,
    ResearchCandidate,
    ResearchDisposition,
    ResearchEvidenceKind,
    ResearchRegistration,
)
from tree_options.research.pilot.admission import PilotAdmission
from tree_options.research.pilot.store import PilotStore
from tree_options.research.pilot.usage import UsageMeter
from tree_options.research.runstate.spec_hash import spec_hash
from tree_options.research.runstate.store import open_runstate_store
from tree_options.research.runstate.worker import RUN_FORMAT_VERSION, ResearchWorker
from tree_options.trex.contracts import wave3 as w

REPO = Path(__file__).resolve().parents[2]
DRAFTS = REPO / "data" / "research" / "pilot"
OWNER = "owner@example.com"
OWNERS = frozenset({OWNER})
T0 = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
DAY = date(2026, 10, 22)
BASELINE = "eq-baseline-spy-weekly"
MIB = 1024 * 1024


def _candidate() -> ResearchCandidate:
    return ResearchCandidate(
        id="c1",
        family="f",
        version="v1",
        evidence_kind=ResearchEvidenceKind.SYNTHETIC_BACKTEST,
        registration=ResearchRegistration.BEFORE_ENTRY_WINDOW_END,
        disposition=ResearchDisposition.PASS,
        plot_funded_account=True,
        supported_start=date(2024, 1, 2),
        supported_end=date(2024, 1, 31),
        funded_history=FundedHistorySupport.RECONSTRUCTED,
    )


def _spec() -> ComparisonSpec:
    return ComparisonSpec(
        candidate_ids=("c1",),
        starting_capital=Decimal("10000"),
        common_start=date(2024, 1, 2),
        common_end=date(2024, 1, 8),
        cashflow_timing=CashflowTiming.BEGINNING_OF_PERIOD,
        contribution_per_period=Decimal("0"),
        cost_model_kind=CostModelKind.FIVE_BP_FIXED,
        benchmark_candidate_id=None,
        currency=Currency.USD,
        price_basis=PriceBasis.NOMINAL_PRETAX,
        idle_cash_policy=IdleCashPolicy.CASH_YIELDS_ZERO,
        rebalancing=Rebalancing.NONE,
        position_sizing=PositionSizing.INTEGER,
        collateral=CollateralPolicy.NONE,
        borrowing=BorrowingPolicy.NONE,
        knowledge_cutoff=datetime(2024, 1, 31, tzinfo=UTC),
        proposed_by="operator",
        notes="",
    )


def spawn(workspace: Path, **fields: Any) -> str:
    spec = _spec()
    run_id = spec_hash(spec)
    workspace.mkdir(parents=True, exist_ok=True)
    with open_runstate_store(workspace) as store:
        store.put("spec", spec.to_dict(), key=run_id, at=datetime.now())
        store.put(
            "run",
            {
                "run_id": run_id,
                "spec_hash": run_id,
                "kind": "comparison",
                "status": "queued",
                "format_version": RUN_FORMAT_VERSION,
                **fields,
            },
            key=run_id,
            at=datetime.now(),
        )
    return run_id


def record(workspace: Path, kind: str, run_id: str) -> dict[str, Any] | None:
    with open_runstate_store(workspace) as store:
        return store.get(kind, run_id)


class Engine:
    """``run_comparison`` that counts its calls (or raises)."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.calls = 0
        self.raises = raises

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return run_comparison(*args, **kwargs)


def ticks(*values: float) -> Any:
    it: Iterator[float] = iter(values)
    return lambda: next(it)


class Pilot:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.store = PilotStore(root)
        self.store.import_drafts(DRAFTS)
        self.meter = UsageMeter(root)

    def seal(self, program_id: str, by: str = OWNER) -> w.PilotRegistration:
        draft = self.store.state(program_id).registration
        assert draft is not None
        return self.store.seal(
            program_id,
            reviewed_sha256=w.registration_body_sha256(draft),
            sealed_by=by,
            owners=frozenset({by}),
            at=T0,
        )

    def usage(self, program_id: str = BASELINE, day: date = DAY) -> dict[str, Any] | None:
        path = self.root / "usage" / program_id / f"{day.isoformat()}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def admission(self, owners: frozenset[str] = OWNERS, at: datetime = T0) -> PilotAdmission:
        return PilotAdmission(self.root, owners=owners, clock=lambda: at)


@pytest.fixture()
def pilot(tmp_path: Path) -> Pilot:
    return Pilot(tmp_path / "pilot")


def worker(tmp_path: Path, engine: Engine, admission: Any, **kw: Any) -> ResearchWorker:
    return ResearchWorker(
        workspace=tmp_path / "ws",
        catalog_provider=lambda: [_candidate()],
        engine_fn=engine,
        admission=admission,
        **kw,
    )


def assert_refused(run: dict[str, Any] | None, blockers: list[str]) -> None:
    assert run is not None
    assert run["status"] == "failed"
    assert run["error_code"] == "pilot_admission_refused"
    assert run["admission_blockers"] == blockers
    assert run["error"] == "pilot_admission_refused: " + ", ".join(blockers)


# -- refusals before any compute ---------------------------------------------------


def test_a_draft_cannot_run(tmp_path: Path, pilot: Pilot) -> None:
    run_id = spawn(tmp_path / "ws", pilot_program_id=BASELINE)
    engine = Engine()
    assert worker(tmp_path, engine, pilot.admission()).step() is True
    assert_refused(record(tmp_path / "ws", "run", run_id), ["registration_not_sealed"])
    assert engine.calls == 0
    assert record(tmp_path / "ws", "result", run_id) is None
    assert pilot.usage() is None  # a refused job is charged nothing


def test_an_unregistered_program_cannot_run(tmp_path: Path, pilot: Pilot) -> None:
    run_id = spawn(tmp_path / "ws", pilot_program_id="eq-never-registered")
    engine = Engine()
    worker(tmp_path, engine, pilot.admission()).step()
    assert_refused(record(tmp_path / "ws", "run", run_id), ["registration_missing"])
    assert engine.calls == 0


def test_an_invalid_program_id_cannot_run(tmp_path: Path, pilot: Pilot) -> None:
    run_id = spawn(tmp_path / "ws", pilot_program_id="../escape")
    engine = Engine()
    worker(tmp_path, engine, pilot.admission()).step()
    assert_refused(record(tmp_path / "ws", "run", run_id), ["pilot_program_invalid"])
    assert engine.calls == 0


@pytest.mark.parametrize(
    ("used", "blocker"),
    [
        ({"requests": 20}, "budget_requests_exhausted"),
        ({"compute_seconds": 10 * 60}, "budget_compute_exhausted"),
        ({"storage_bytes": 64 * MIB}, "budget_storage_exhausted"),
    ],
    ids=["requests", "compute", "storage"],
)
def test_an_over_budget_job_is_refused_at_admission(
    tmp_path: Path, pilot: Pilot, used: dict[str, int], blocker: str
) -> None:
    pilot.seal(BASELINE)
    pilot.meter.charge(BASELINE, DAY, at=T0, **used)
    before = pilot.usage()
    run_id = spawn(tmp_path / "ws", pilot_program_id=BASELINE)
    engine = Engine()
    worker(tmp_path, engine, pilot.admission()).step()
    assert_refused(record(tmp_path / "ws", "run", run_id), [blocker])
    assert engine.calls == 0
    assert pilot.usage() == before


def test_one_unit_below_every_budget_is_admitted(tmp_path: Path, pilot: Pilot) -> None:
    pilot.seal(BASELINE)
    pilot.meter.charge(
        BASELINE, DAY, requests=19, compute_seconds=599, storage_bytes=64 * MIB - 1, at=T0
    )
    run_id = spawn(tmp_path / "ws", pilot_program_id=BASELINE)
    worker(tmp_path, Engine(), pilot.admission(), monotonic=ticks(0.0, 1.0)).step()
    run = record(tmp_path / "ws", "run", run_id)
    assert run is not None and run["status"] == "completed"


def test_unknown_usage_refuses(tmp_path: Path, pilot: Pilot) -> None:
    pilot.seal(BASELINE)
    path = pilot.root / "usage" / BASELINE / "2026-10-22.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{broken")
    run_id = spawn(tmp_path / "ws", pilot_program_id=BASELINE)
    engine = Engine()
    worker(tmp_path, engine, pilot.admission()).step()
    assert_refused(record(tmp_path / "ws", "run", run_id), ["budget_usage_unknown"])
    assert engine.calls == 0
    assert path.read_bytes() == b"{broken"


def test_a_seal_by_someone_no_longer_an_owner_refuses(tmp_path: Path, pilot: Pilot) -> None:
    pilot.seal(BASELINE)
    run_id = spawn(tmp_path / "ws", pilot_program_id=BASELINE)
    engine = Engine()
    worker(tmp_path, engine, pilot.admission(owners=frozenset({"new@example.com"}))).step()
    assert_refused(record(tmp_path / "ws", "run", run_id), ["registration_sealer_not_owner"])
    assert engine.calls == 0


def test_another_program_job_in_flight_refuses(tmp_path: Path, pilot: Pilot) -> None:
    pilot.seal(BASELINE)
    pilot.meter.open_job(BASELINE, "f" * 64, DAY, at=T0)
    run_id = spawn(tmp_path / "ws", pilot_program_id=BASELINE)
    engine = Engine()
    worker(tmp_path, engine, pilot.admission()).step()
    assert_refused(record(tmp_path / "ws", "run", run_id), ["budget_usage_unknown"])
    assert engine.calls == 0


# -- admitted jobs are metered -----------------------------------------------------


def test_an_admitted_job_is_metered(tmp_path: Path, pilot: Pilot) -> None:
    pilot.seal(BASELINE)
    run_id = spawn(tmp_path / "ws", pilot_program_id=BASELINE)
    engine = Engine()
    worker(tmp_path, engine, pilot.admission(), monotonic=ticks(100.0, 142.4)).step()
    run = record(tmp_path / "ws", "run", run_id)
    assert run is not None and run["status"] == "completed"
    assert engine.calls == 1
    assert run["pilot_day"] == "2026-10-22"
    assert run["pilot_admitted_at"] == "2026-10-22T14:00:00Z"
    result = record(tmp_path / "ws", "result", run_id)
    assert result is not None
    stored = len(
        json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )
    usage = pilot.usage()
    assert usage is not None
    assert (usage["requests"], usage["compute_seconds"], usage["storage_bytes"]) == (
        1,
        43,  # 42.4 s of compute, rounded up to whole seconds
        stored,
    )
    assert pilot.meter.job(BASELINE, run_id) is None


def test_a_failed_job_is_metered_for_its_compute(tmp_path: Path, pilot: Pilot) -> None:
    pilot.seal(BASELINE)
    run_id = spawn(tmp_path / "ws", pilot_program_id=BASELINE)
    engine = Engine(raises=RuntimeError("engine fell over"))
    worker(tmp_path, engine, pilot.admission(), monotonic=ticks(10.0, 15.0)).step()
    run = record(tmp_path / "ws", "run", run_id)
    assert run is not None and run["status"] == "failed"
    assert "error_code" not in run
    usage = pilot.usage()
    assert usage is not None
    assert (usage["requests"], usage["compute_seconds"], usage["storage_bytes"]) == (1, 5, 0)
    assert pilot.meter.job(BASELINE, run_id) is None


def test_usage_is_charged_to_the_et_day_of_admission(tmp_path: Path, pilot: Pilot) -> None:
    pilot.seal(BASELINE)
    late = datetime(2026, 10, 23, 3, 30, tzinfo=UTC)  # 23:30 ET on 2026-10-22
    early = datetime(2026, 10, 22, 3, 30, tzinfo=UTC)  # 23:30 ET on 2026-10-21
    spawn(tmp_path / "ws", pilot_program_id=BASELINE)
    worker(tmp_path, Engine(), pilot.admission(at=late), monotonic=ticks(0.0, 2.0)).step()
    usage = pilot.usage(day=date(2026, 10, 22))
    assert usage is not None and usage["requests"] == 1
    assert pilot.usage(day=date(2026, 10, 23)) is None
    adm = pilot.admission(at=early)
    blockers, fields = adm.admit({"run_id": "b" * 64, "pilot_program_id": BASELINE})
    assert (blockers, fields["pilot_day"]) == ((), "2026-10-21")


def test_two_jobs_of_one_program_run_one_at_a_time(pilot: Pilot) -> None:
    pilot.seal(BASELINE)
    adm = pilot.admission()
    first = {"run_id": "1" * 64, "pilot_program_id": BASELINE}
    second = {"run_id": "2" * 64, "pilot_program_id": BASELINE}
    blockers, fields = adm.admit(first)
    assert blockers == ()
    assert adm.admit(second) == (("budget_usage_unknown",), {})
    adm.settle({**first, **fields}, compute_seconds=7, storage_bytes=3)
    assert adm.admit(second)[0] == ()
    usage = pilot.usage()
    assert usage is not None and (usage["requests"], usage["compute_seconds"]) == (2, 7)


def test_an_interrupted_job_is_charged_its_elapsed_time_when_readmitted(pilot: Pilot) -> None:
    pilot.seal(BASELINE)
    run = {"run_id": "c" * 64, "pilot_program_id": BASELINE}
    assert pilot.admission(at=T0).admit(run)[0] == ()
    # the worker died mid-job; at restart the run is requeued and re-admitted
    restart = datetime(2026, 10, 22, 14, 5, tzinfo=UTC)
    blockers, _fields = pilot.admission(at=restart).admit(run)
    assert blockers == ()
    usage = pilot.usage()
    assert usage is not None and (usage["requests"], usage["compute_seconds"]) == (2, 300)
    job = pilot.meter.job(BASELINE, run["run_id"])
    assert job is not None and job.admitted_at == restart


def test_settle_is_idempotent(pilot: Pilot) -> None:
    pilot.seal(BASELINE)
    adm = pilot.admission()
    run = {"run_id": "d" * 64, "pilot_program_id": BASELINE}
    _blockers, fields = adm.admit(run)
    adm.settle({**run, **fields}, compute_seconds=4, storage_bytes=0)
    adm.settle({**run, **fields}, compute_seconds=4, storage_bytes=0)
    usage = pilot.usage()
    assert usage is not None and usage["compute_seconds"] == 4


# -- the hook only touches pilot runs ----------------------------------------------


class Exploding:
    def admit(self, run: Any) -> Any:
        raise AssertionError("a run without pilot_program_id never meets admission")

    def settle(self, run: Any, **kw: Any) -> None:
        raise AssertionError("a run without pilot_program_id is never metered")


def test_runs_without_a_pilot_program_are_untouched(tmp_path: Path) -> None:
    run_id = spawn(tmp_path / "ws")
    engine = Engine()
    worker(tmp_path, engine, Exploding()).step()
    run = record(tmp_path / "ws", "run", run_id)
    assert run is not None and run["status"] == "completed"
    assert "pilot_day" not in run and "admission_blockers" not in run
    assert engine.calls == 1


def test_a_pilot_run_without_explicit_admission_uses_the_env_pilot_dir(
    tmp_path: Path, pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TREX_PILOT_DIR", str(pilot.root))
    monkeypatch.setenv("TREX_OWNER_EMAILS", OWNER)
    run_id = spawn(tmp_path / "ws", pilot_program_id=BASELINE)
    engine = Engine()
    worker(tmp_path, engine, None).step()
    assert_refused(record(tmp_path / "ws", "run", run_id), ["registration_not_sealed"])
    assert engine.calls == 0


def test_a_broken_admission_refuses_rather_than_runs(tmp_path: Path) -> None:
    class Broken:
        def admit(self, run: Any) -> Any:
            raise OSError("pilot dir unreadable")

        def settle(self, run: Any, **kw: Any) -> None:
            raise AssertionError("never admitted, never settled")

    run_id = spawn(tmp_path / "ws", pilot_program_id=BASELINE)
    engine = Engine()
    worker(tmp_path, engine, Broken()).step()
    assert_refused(record(tmp_path / "ws", "run", run_id), ["pilot_admission_unavailable"])
    assert engine.calls == 0
