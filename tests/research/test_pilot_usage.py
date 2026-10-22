"""Wave 4 lane I: ``trex-pilot-usage/1`` metering (C3.5, C3.7).

One file per program per ET day, ``usage/<program_id>/<YYYY-MM-DD>.json``.
Unknown usage is never read as zero: an unreadable, invalid or foreign file,
or a job whose compute is not yet settled, reads as unknown with a reason.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tree_options.research.pilot.store import PilotError
from tree_options.research.pilot.usage import UsageMeter
from tree_options.trex.contracts import wave3 as w

DAY = date(2026, 10, 22)
T0 = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 22, 15, 30, tzinfo=UTC)
P = "eq-baseline-spy-weekly"
RUN = "a" * 64


def day_file(root: Path, program_id: str = P, day: date = DAY) -> Path:
    return root / "usage" / program_id / f"{day.isoformat()}.json"


def test_charges_accumulate_in_one_file_per_program_and_day(tmp_path: Path) -> None:
    meter = UsageMeter(tmp_path)
    meter.charge(P, DAY, requests=1, at=T0)
    meter.charge(P, DAY, requests=2, compute_seconds=30, storage_bytes=100, at=T1)
    document = json.loads(day_file(tmp_path).read_text())
    assert document == {
        "schema": "trex-pilot-usage/1",
        "program_id": P,
        "day": "2026-10-22",
        "requests": 3,
        "compute_seconds": 30,
        "storage_bytes": 100,
        "as_of": "2026-10-22T15:30:00Z",
    }
    w.BudgetUsage.model_validate(document)
    other = date(2026, 10, 23)
    meter.charge(P, other, requests=1, at=T1)
    assert json.loads(day_file(tmp_path, day=other).read_text())["requests"] == 1
    assert json.loads(day_file(tmp_path).read_text())["requests"] == 3


def test_a_day_without_any_admission_reads_as_zero_metered_usage(tmp_path: Path) -> None:
    reading = UsageMeter(tmp_path).read(P, DAY, now=T0)
    assert reading.reason is None
    assert reading.usage == w.BudgetUsage(
        program_id=P, day=DAY, requests=0, compute_seconds=0, storage_bytes=0, as_of=T0
    )
    assert not day_file(tmp_path).exists()  # reading never writes


@pytest.mark.parametrize(
    ("content", "detail"),
    [
        (b"{broken", "unreadable"),
        (b'{"schema": "trex-pilot-usage/1"}', "invalid"),
        (
            json.dumps(
                {
                    "schema": "trex-pilot-usage/1",
                    "program_id": "another-program",
                    "day": "2026-10-22",
                    "requests": 0,
                    "compute_seconds": 0,
                    "storage_bytes": 0,
                    "as_of": "2026-10-22T14:00:00Z",
                }
            ).encode(),
            "mismatch",
        ),
        (
            json.dumps(
                {
                    "schema": "trex-pilot-usage/1",
                    "program_id": P,
                    "day": "2026-10-21",
                    "requests": 0,
                    "compute_seconds": 0,
                    "storage_bytes": 0,
                    "as_of": "2026-10-22T14:00:00Z",
                }
            ).encode(),
            "mismatch",
        ),
    ],
    ids=["garbage", "schema-invalid", "foreign-program", "foreign-day"],
)
def test_unknown_usage_is_never_read_as_zero(tmp_path: Path, content: bytes, detail: str) -> None:
    path = day_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    reading = UsageMeter(tmp_path).read(P, DAY, now=T0)
    assert reading.usage is None
    assert reading.reason == f"budget_usage_unknown:{detail}"


def test_charging_unknown_usage_refuses_and_keeps_the_evidence(tmp_path: Path) -> None:
    path = day_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{broken")
    with pytest.raises(PilotError) as refusal:
        UsageMeter(tmp_path).charge(P, DAY, requests=1, at=T0)
    assert refusal.value.code == "budget_usage_unknown"
    assert path.read_bytes() == b"{broken"


def test_a_job_in_flight_makes_the_program_usage_unknown(tmp_path: Path) -> None:
    meter = UsageMeter(tmp_path)
    meter.charge(P, DAY, requests=1, at=T0)
    meter.open_job(P, RUN, DAY, at=T0)
    reading = meter.read(P, DAY, now=T1)
    assert (reading.usage, reading.reason) == (None, "budget_usage_unknown:job_in_flight")
    job = meter.job(P, RUN)
    assert job is not None and (job.day, job.admitted_at) == (DAY, T0)
    meter.close_job(P, RUN)
    assert meter.job(P, RUN) is None
    assert meter.read(P, DAY, now=T1).usage is not None


def test_a_job_marker_is_opened_once(tmp_path: Path) -> None:
    meter = UsageMeter(tmp_path)
    meter.open_job(P, RUN, DAY, at=T0)
    with pytest.raises(FileExistsError):
        meter.open_job(P, RUN, DAY, at=T1)


def test_negative_charges_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        UsageMeter(tmp_path).charge(P, DAY, compute_seconds=-1, at=T0)
    assert not day_file(tmp_path).exists()


def test_the_usage_dir_must_be_a_directory(tmp_path: Path) -> None:
    (tmp_path / "usage").write_text("not a dir")
    reading = UsageMeter(tmp_path).read(P, DAY, now=T0)
    assert reading.usage is None
    assert reading.reason == "budget_usage_unknown:unreadable"
