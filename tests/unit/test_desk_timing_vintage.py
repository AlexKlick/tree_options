"""The earnings-timing vintage store (plan D6 look-ahead guard).

The market lane's ``earnings-timing.json`` is rewritten in place every week
(estimates move, get confirmed or deleted), so a historical decision cannot
read it. The miner snapshots it once per session into
``DESK_STORE/earnings-timing/<D>.json``, only BEFORE D's decision cutoff
(the next session's 09:30 ET), never rewritten; a decision for D reads the
newest vintage dated on or before D. Oracles are literal documents and
instants written here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.fixtures import desk_pricing as fx
from tree_options.desk import pit
from tree_options.time.calendar import StaticSessionCalendar

ET = ZoneInfo("America/New_York")
D = date(2025, 3, 12)  # a Wednesday: its cutoff is 2025-03-13 09:30 EDT
BEFORE = datetime(2025, 3, 13, 7, 15, tzinfo=ET)
AFTER = datetime(2025, 3, 13, 9, 31, tzinfo=ET)


def _t(timing: str, status: str, fetched_at: str) -> dict[str, str]:
    return {"timing": timing, "status": status, "fetched_at": fetched_at, "source": "test"}


LIVE = {"AAA": {"2025-04-10": _t("bmo", "estimated", "2025-03-03T21:00:00-05:00")}}


@pytest.fixture(scope="module")
def cal() -> StaticSessionCalendar:
    return fx.trex_calendar()


def _paper(tmp_path: Path, doc: Any) -> Path:
    paper = tmp_path / "paper"
    paper.mkdir(exist_ok=True)
    text = doc if isinstance(doc, str) else json.dumps(doc, indent=1, sort_keys=True)
    (paper / "earnings-timing.json").write_text(text)
    return paper


def _vintage_file(store: Path, d: date) -> Path:
    return store / "earnings-timing" / f"{d.isoformat()}.json"


def test_snapshot_before_the_cutoff_is_written_once_and_never_rewritten(
    cal: StaticSessionCalendar, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    paper = _paper(tmp_path, LIVE)
    status, v = pit.snapshot_timing(D, cal, paper=paper, store=store, now=BEFORE)
    assert status == "written" and v is not None
    assert v.session == D and v.timing == LIVE and v.snapshot_at == BEFORE
    live_bytes = (paper / "earnings-timing.json").read_bytes()
    assert v.source_sha256 == hashlib.sha256(live_bytes).hexdigest()
    doc = json.loads(_vintage_file(store, D).read_text())
    assert doc["schema"] == "desk-earnings-timing-vintage/1"
    assert doc["session"] == "2025-03-12" and doc["timing"] == LIVE
    first = _vintage_file(store, D).read_bytes()
    # the vendor moves the estimate later that morning: the vintage stands
    moved = {"AAA": {"2025-04-17": _t("bmo", "estimated", "2025-03-13T08:00:00-04:00")}}
    _paper(tmp_path, moved)
    status2, v2 = pit.snapshot_timing(
        D, cal, paper=paper, store=store, now=datetime(2025, 3, 13, 8, 30, tzinfo=ET)
    )
    assert status2 == "exists" and v2 is not None and v2.timing == LIVE
    assert _vintage_file(store, D).read_bytes() == first


def test_snapshot_after_the_cutoff_is_refused(cal: StaticSessionCalendar, tmp_path: Path) -> None:
    store = tmp_path / "store"
    status, v = pit.snapshot_timing(D, cal, paper=_paper(tmp_path, LIVE), store=store, now=AFTER)
    assert (status, v) == ("too_late", None)
    assert not _vintage_file(store, D).exists()


def test_dry_run_returns_the_vintage_without_writing(
    cal: StaticSessionCalendar, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    status, v = pit.snapshot_timing(
        D, cal, paper=_paper(tmp_path, LIVE), store=store, now=BEFORE, dry_run=True
    )
    assert status == "dry_run" and v is not None and v.timing == LIVE
    assert not (store / "earnings-timing").exists()


def test_missing_live_file_snapshots_an_empty_schedule(
    cal: StaticSessionCalendar, tmp_path: Path
) -> None:
    paper = tmp_path / "paper"
    paper.mkdir()
    status, v = pit.snapshot_timing(D, cal, paper=paper, store=tmp_path / "s", now=BEFORE)
    assert status == "written" and v is not None and v.timing == {}
    assert v.source_sha256 == ""


def test_malformed_live_file_is_not_snapshotted(cal: StaticSessionCalendar, tmp_path: Path) -> None:
    paper = _paper(tmp_path, {"AAA": {"2025-04-10": {"timing": "noon"}}})
    with pytest.raises(pit.NotEvaluable, match="timing"):
        pit.snapshot_timing(D, cal, paper=paper, store=tmp_path / "s", now=BEFORE)
    assert not (tmp_path / "s" / "earnings-timing").exists()


def _write_vintage(store: Path, d: date, snapshot_at: str, timing: Any) -> None:
    path = _vintage_file(store, d)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "desk-earnings-timing-vintage/1",
                "session": d.isoformat(),
                "snapshot_at": snapshot_at,
                "source": "earnings-timing.json",
                "source_sha256": "ab" * 32,
                "timing": timing,
            }
        )
    )


def test_a_decision_reads_the_newest_vintage_on_or_before_its_session(
    cal: StaticSessionCalendar, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    older = {"AAA": {"2025-04-03": _t("amc", "estimated", "2025-03-01T21:00:00-05:00")}}
    _write_vintage(store, date(2025, 3, 10), "2025-03-11T07:15:00-04:00", older)
    _write_vintage(store, date(2025, 3, 11), "2025-03-12T07:15:00-04:00", LIVE)
    # dated after D: what D could not know
    _write_vintage(store, date(2025, 3, 13), "2025-03-14T07:15:00-04:00", {"AAA": {}})
    got = pit.load_timing_vintage(D, cal, store=store)
    assert got is not None and got.session == date(2025, 3, 11) and got.timing == LIVE
    assert pit.load_timing_vintage(date(2025, 3, 10), cal, store=store).timing == older  # type: ignore[union-attr]
    assert pit.load_timing_vintage(date(2025, 3, 7), cal, store=store) is None
    # the sha names the timing content, so a queue can say what it read
    want = hashlib.sha256(json.dumps(LIVE, sort_keys=True).encode()).hexdigest()
    assert got.sha256 == want


def test_a_vintage_snapshotted_after_its_own_cutoff_is_refused(
    cal: StaticSessionCalendar, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    _write_vintage(store, date(2025, 3, 11), "2025-03-12T09:31:00-04:00", LIVE)
    with pytest.raises(pit.NotEvaluable, match="after its decision cutoff"):
        pit.load_timing_vintage(D, cal, store=store)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema="other/1"),
        lambda d: d.update(session="2025-03-10"),
        lambda d: d.update(snapshot_at="2025-03-12T07:15:00"),  # naive: proves nothing
        lambda d: d.update(timing={"AAA": {"2025-04-10": {"timing": "noon"}}}),
    ],
)
def test_a_malformed_vintage_fails_closed(
    cal: StaticSessionCalendar, tmp_path: Path, mutate: Any
) -> None:
    store = tmp_path / "store"
    _write_vintage(store, date(2025, 3, 11), "2025-03-12T07:15:00-04:00", LIVE)
    path = _vintage_file(store, date(2025, 3, 11))
    doc = json.loads(path.read_text())
    mutate(doc)
    path.write_text(json.dumps(doc))
    with pytest.raises(pit.NotEvaluable):
        pit.load_timing_vintage(D, cal, store=store)


def test_load_sources_takes_the_vintage_instead_of_the_live_file(tmp_path: Path) -> None:
    paper = tmp_path / "paper"
    paper.mkdir()
    (paper / "ohlc-panel.json").write_text(json.dumps({"SPY": {}}))
    (paper / "earnings-calendar.json").write_text(json.dumps({"AAA": []}))
    (paper / "earnings-timing.json").write_text("{not json")  # never read
    src = pit.load_sources(paper=paper, store=tmp_path / "store", timing=LIVE)
    assert src.timing == LIVE
