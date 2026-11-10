"""AI-BEAR-001 history fetcher: FRED/Yahoo parsing and fail-closed storage."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tree_options.desk.aibear import history as h


def _fred(rows: list[tuple[str, str]], series: str = "NASDAQ100") -> bytes:
    return ("observation_date," + series + "\n" + "".join(f"{d},{v}\n" for d, v in rows)).encode()


def _yahoo(points: list[tuple[int, float | None]], offset: int = -18000) -> bytes:
    return json.dumps(
        {
            "chart": {
                "result": [
                    {
                        "meta": {"gmtoffset": offset},
                        "timestamp": [t for t, _ in points],
                        "indicators": {
                            "quote": [{"close": [999.0 for _ in points]}],
                            "adjclose": [{"adjclose": [c for _, c in points]}],
                        },
                    }
                ]
            }
        }
    ).encode()


def test_fred_rows_without_a_value_are_skipped() -> None:
    got = h.parse_fred_csv(
        _fred([("2001-02-02", "54.89"), ("2001-02-05", "."), ("2001-02-06", "")]), "NASDAQ100"
    )
    assert got == [(date(2001, 2, 2), 54.89)]


def test_fred_header_must_name_the_series() -> None:
    with pytest.raises(h.HistoryParseError):
        h.parse_fred_csv(_fred([("2001-02-02", "1")], "OTHER"), "NASDAQ100")


def test_yahoo_uses_adjusted_closes_in_exchange_time_and_skips_nulls() -> None:
    # 2000-01-03 14:30 UTC is 09:30 ET the same day; 2000-01-05 04:00 UTC
    # is still 2000-01-04 in ET (offset -5 h)
    t1 = int(datetime(2000, 1, 3, 14, 30, tzinfo=UTC).timestamp())
    t2 = int(datetime(2000, 1, 5, 4, 0, tzinfo=UTC).timestamp())
    t3 = t1 + 3 * 86400
    got = h.parse_yahoo_chart(_yahoo([(t1, 10.5), (t2, 11.0), (t3, None)]), "X")
    assert got == [(date(2000, 1, 3), 10.5), (date(2000, 1, 4), 11.0)]


def test_a_non_positive_close_is_refused() -> None:
    with pytest.raises(h.HistoryParseError):
        h.parse_yahoo_chart(_yahoo([(946909800, 0.0)]), "X")


def _days(n: int, start: date) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_fetch_records_completed_sessions_only(tmp_path: Path) -> None:
    now = datetime(2026, 11, 9, 21, 5, tzinfo=UTC)  # 16:05 ET on 11-09
    days = [*_days(299, date(2025, 9, 1)), date(2026, 11, 9)]
    body = _fred([(d.isoformat(), "100") for d in days])

    def get(url: str, *, headers, timeout):
        assert headers["Accept"] == "*/*"
        return 200, body

    line = h.fetch_one(h.SOURCES[0], get=get, root=tmp_path, now=now)
    assert line["status"] == "recorded"
    assert line["dropped_in_progress"] == 1
    stored = h.read_history("NDX", tmp_path)
    assert stored[-1][0] < date(2026, 11, 9)
    assert len(stored) == 299


def test_an_implausible_fetch_never_replaces_the_stored_file(tmp_path: Path) -> None:
    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    good = _fred([(d.isoformat(), "100") for d in _days(300, date(2025, 1, 2))])
    h.fetch_one(h.SOURCES[0], get=lambda u, **k: (200, good), root=tmp_path, now=now)
    before = (tmp_path / "history" / "NDX.csv").read_bytes()
    short = _fred([("2026-01-02", "5")])
    line = h.fetch_one(h.SOURCES[0], get=lambda u, **k: (200, short), root=tmp_path, now=now)
    assert line["status"] == "invalid"
    assert (tmp_path / "history" / "NDX.csv").read_bytes() == before
    gone = h.fetch_one(h.SOURCES[0], get=lambda u, **k: (503, b""), root=tmp_path, now=now)
    assert gone["status"] == "missing"


def test_transport_errors_are_recorded_gaps(tmp_path: Path) -> None:
    def boom(url: str, **kw):
        raise TimeoutError("slow")

    lines = h.fetch_history(only=["NDX"], get=boom, root=tmp_path, sleep=lambda s: None)
    assert lines[0]["status"] == "error"
    prov = (tmp_path / "history" / "provenance.jsonl").read_text().splitlines()
    assert json.loads(prov[0])["status"] == "error"


def test_unknown_sources_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        h.fetch_history(only=["NOPE"], root=tmp_path)
