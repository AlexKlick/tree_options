"""Desk D1 indices: CBOE index-history CSVs and FRED DTB3 (parse, normalize,
store, vendor-revision detection, idempotency, lag, exit codes, CLI).

No network: every fetch goes through an injected ``get``. The fixtures in
``tests/fixtures/desk_indices/`` are trimmed captures of one real request
each (2026-09-23). Every store/state path is pinned to tmp.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tree_options.desk import indices
from tree_options.desk.__main__ import run_cli

ET = ZoneInfo("America/New_York")
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "desk_indices"
MORNING = datetime(2026, 9, 23, 7, 0, tzinfo=ET)  # latest completed session: 09-22
EVENING = datetime(2026, 9, 23, 19, 0, tzinfo=ET)  # latest completed session: 09-23

VIX = (FIX / "VIX_History.csv").read_bytes()
VVIX = (FIX / "VVIX_History.csv").read_bytes()
DTB3 = (FIX / "fredgraph_DTB3.csv").read_bytes()

# the normalized store text for the VIX fixture, written out by hand
VIX_NORMALIZED = (
    "date,open,high,low,close\n"
    "1990-01-02,17.240000,17.240000,17.240000,17.240000\n"
    "1990-01-03,18.190000,18.190000,18.190000,18.190000\n"
    "2026-09-16,16.910000,18.940000,16.400000,17.710000\n"
    "2026-09-17,16.030000,16.290000,15.380000,15.440000\n"
    "2026-09-18,15.070000,15.630000,14.800000,14.810000\n"
    "2026-09-21,14.960000,15.130000,14.600000,14.870000\n"
    "2026-09-22,14.640000,14.950000,14.190000,14.210000\n"
)


@pytest.fixture(autouse=True)
def _pinned_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DESK_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("TREX_DESK_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("DESK_PAPER_DIR", str(tmp_path / "paper"))
    monkeypatch.setenv("TREX_NOTIFY_ENV", str(tmp_path / "no-notify.env"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DESK_REPO_ROOT", raising=False)


def _key(url: str) -> str:
    if "fred.stlouisfed.org" in url:
        return url.rsplit("id=", 1)[1]
    return url.rsplit("/", 1)[1].removesuffix("_History.csv")


def _get(bodies: dict[str, bytes | int | Exception], calls: list[tuple[str, dict]] | None = None):
    """source name -> body; an int is an HTTP status, an Exception raises."""

    def get(url: str, *, headers, timeout: float) -> tuple[int, bytes]:
        if calls is not None:
            calls.append((url, dict(headers)))
        body = bodies.get(_key(url), 404)
        if isinstance(body, Exception):
            raise body
        if isinstance(body, int):
            return body, b""
        return 200, body

    return get


def _src(*names: str) -> tuple[indices.Source, ...]:
    return tuple(s for s in indices.SOURCES if s.name in names)


def _run(
    tmp_path: Path,
    bodies: dict[str, bytes | int | Exception],
    static_calendar,
    *,
    now: datetime = MORNING,
    dry_run: bool = False,
    calls: list | None = None,
    slept: list | None = None,
) -> indices.IndicesSummary:
    return indices.record_indices(
        _src(*bodies),
        root=tmp_path / "store",
        get=_get(bodies, calls),
        clock=lambda: now,
        sleep=(slept.append if slept is not None else lambda _s: None),
        cal=static_calendar,
        dry_run=dry_run,
    )


def _prov(tmp_path: Path) -> list[dict]:
    path = tmp_path / "store" / "indices" / "provenance.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()]


# ------------------------------------------------------------------ sources


class TestSources:
    def test_the_probed_sources_and_urls(self) -> None:
        names = [s.name for s in indices.SOURCES]
        assert names == [
            "VIX",
            "VIX9D",
            "VIX1D",
            "VIX3M",
            "VIX6M",
            "VIX1Y",
            "VVIX",
            "SKEW",
            "VXN",
            "RVX",
            "GVZ",
            "VXAPL",
            "VXAZN",
            "VXGOG",
            "DTB3",
        ]
        by = {s.name: s for s in indices.SOURCES}
        assert by["VIX"].url == (
            "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
        )
        assert by["DTB3"].url == "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3"
        assert by["VIX"].kind == "cboe" and by["DTB3"].kind == "fred"


# ------------------------------------------------------------------ parsing


class TestParse:
    def test_cboe_ohlc_rows_normalized_values_as_served(self) -> None:
        rows = indices.parse_csv(VIX, "cboe", "VIX")
        assert len(rows) == 7
        assert rows[0] == ("1990-01-02", "17.240000", "17.240000", "17.240000", "17.240000")
        assert rows[-1] == ("2026-09-22", "14.640000", "14.950000", "14.190000", "14.210000")

    def test_cboe_single_value_series_fills_close_only(self) -> None:
        rows = indices.parse_csv(VVIX, "cboe", "VVIX")
        assert rows[0] == ("2006-03-06", "", "", "", "71.730000")
        assert rows[-1] == ("2026-09-22", "", "", "", "83.170000")

    def test_fred_missing_values_stay_empty_never_filled(self) -> None:
        rows = indices.parse_csv(DTB3, "fred", "DTB3")
        by = {r[0]: r for r in rows}
        assert by["2026-09-07"] == ("2026-09-07", "", "", "", "")  # Labor Day: no value
        assert by["2026-09-08"] == ("2026-09-08", "", "", "", "3.80")
        assert rows[-1] == ("2026-09-22", "", "", "", "4.01")
        assert len(rows) == 9

    @pytest.mark.parametrize(
        ("body", "kind"),
        [
            (b"", "cboe"),
            (b"DATE,OPEN,HIGH,LOW,CLOSE\n", "cboe"),  # header only: no history
            (b"<html>Access Denied</html>\n", "cboe"),
            (b"DATE,OPEN,HIGH,LOW,CLOSE\n01/02/1990,17.24,x,17.24,17.24\n", "cboe"),
            (b"DATE,OPEN,HIGH,LOW,CLOSE\n13/02/1990,1,1,1,1\n", "cboe"),
            (b"DATE,OPEN,HIGH,LOW,CLOSE\n01/02/1990,1,1,1\n", "cboe"),
            (b"DATE,OPEN,HIGH,LOW,CLOSE\n01/03/1990,1,1,1,1\n01/02/1990,1,1,1,1\n", "cboe"),
            (b"DATE,OPEN,HIGH,LOW,CLOSE\n01/02/1990,1,1,1,1\n01/02/1990,1,1,1,1\n", "cboe"),
            (b"DATE,VIX\n01/02/1990,nan\n", "cboe"),
            (b"observation_date,DGS10\n2026-09-22,4.0\n", "fred"),  # wrong series
            (b"observation_date,DTB3\n2026-09-22,inf\n", "fred"),
        ],
    )
    def test_malformed_payloads_are_refused(self, body: bytes, kind: str) -> None:
        name = "DTB3" if kind == "fred" else "VIX"
        with pytest.raises(indices.IndexParseError):
            indices.parse_csv(body, kind, name)

    def test_single_value_header_must_name_the_series(self) -> None:
        with pytest.raises(indices.IndexParseError):
            indices.parse_csv(b"DATE,SKEW\n01/02/1990,126.09\n", "cboe", "VVIX")


# -------------------------------------------------------------------- store


class TestRecord:
    def test_new_source_writes_normalized_csv_and_provenance(
        self, tmp_path: Path, static_calendar
    ) -> None:
        s = _run(tmp_path, {"VIX": VIX}, static_calendar)
        r = s.results["VIX"]
        assert r.status == "new" and r.rows == 7 and r.last_date == "2026-09-22"
        assert not r.lagging
        path = tmp_path / "store" / "indices" / "VIX.csv"
        assert path.read_text() == VIX_NORMALIZED
        (line,) = _prov(tmp_path)
        assert line["source"] == "VIX" and line["status"] == "new"
        assert line["sha256"] == hashlib.sha256(VIX).hexdigest()
        assert line["rows"] == 7 and line["last_date"] == "2026-09-22"
        assert line["fetched_at"] == MORNING.isoformat()
        assert line["url"].endswith("/VIX_History.csv")
        assert not list((tmp_path / "store").rglob("*.tmp"))
        assert indices.exit_code(s) == 0

    def test_rerun_is_idempotent(self, tmp_path: Path, static_calendar) -> None:
        _run(tmp_path, {"VIX": VIX, "DTB3": DTB3}, static_calendar)
        root = tmp_path / "store" / "indices"
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.glob("*.csv")}
        s = _run(tmp_path, {"VIX": VIX, "DTB3": DTB3}, static_calendar)
        assert {k: r.status for k, r in s.results.items()} == {
            "VIX": "unchanged",
            "DTB3": "unchanged",
        }
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.glob("*.csv")}
        assert after == before
        assert not list(root.glob("*.bak")) and not (root / "changes.jsonl").exists()
        assert [x["status"] for x in _prov(tmp_path)] == ["new", "new", "unchanged", "unchanged"]

    def test_appended_rows_are_an_update_not_a_revision(
        self, tmp_path: Path, static_calendar
    ) -> None:
        _run(tmp_path, {"VIX": VIX}, static_calendar)
        grown = VIX + b"09/23/2026,14.300000,14.900000,14.100000,14.500000\n"
        s = _run(tmp_path, {"VIX": grown}, static_calendar, now=datetime(2026, 9, 24, 7, tzinfo=ET))
        assert s.results["VIX"].status == "updated"
        root = tmp_path / "store" / "indices"
        assert (root / "VIX.csv").read_text() == (
            VIX_NORMALIZED + "2026-09-23,14.300000,14.900000,14.100000,14.500000\n"
        )
        assert not list(root.glob("*.bak")) and not (root / "changes.jsonl").exists()

    def test_vendor_revision_keeps_the_prior_file_and_logs_every_change(
        self, tmp_path: Path, static_calendar
    ) -> None:
        _run(tmp_path, {"VIX": VIX}, static_calendar)
        root = tmp_path / "store" / "indices"
        prior = (root / "VIX.csv").read_bytes()
        revised = VIX.replace(
            b"09/21/2026,14.960000,15.130000,14.600000,14.870000",
            b"09/21/2026,14.960000,15.130000,14.600000,14.880000",
        )
        assert revised != VIX
        s = _run(tmp_path, {"VIX": revised}, static_calendar)
        assert s.results["VIX"].status == "revised"
        assert "1 revised" in s.results["VIX"].detail
        bak = root / "VIX.2026-09-23.bak"  # the fetch date (ET)
        assert bak.read_bytes() == prior
        assert (
            "2026-09-21,14.960000,15.130000,14.600000,14.880000" in (root / "VIX.csv").read_text()
        )
        (change,) = [json.loads(x) for x in (root / "changes.jsonl").read_text().splitlines()]
        assert change["source"] == "VIX" and change["date"] == "2026-09-21"
        assert change["old"] == ["14.960000", "15.130000", "14.600000", "14.870000"]
        assert change["new"] == ["14.960000", "15.130000", "14.600000", "14.880000"]
        assert change["backup"] == "VIX.2026-09-23.bak"
        # a second revision the same day never overwrites the first backup
        again = revised.replace(b"14.880000", b"14.890000")
        s = _run(tmp_path, {"VIX": again}, static_calendar)
        assert s.results["VIX"].status == "revised"
        assert bak.read_bytes() == prior
        baks = sorted(p.name for p in root.glob("VIX.*.bak"))
        assert baks == ["VIX.2026-09-23-2.bak", "VIX.2026-09-23.bak"]
        assert "14.880000" in (root / "VIX.2026-09-23-2.bak").read_text()
        assert indices.exit_code(s) == 0

    def test_a_dropped_past_row_is_a_logged_revision(self, tmp_path: Path, static_calendar) -> None:
        _run(tmp_path, {"VIX": VIX}, static_calendar)
        dropped = VIX.replace(b"09/17/2026,16.030000,16.290000,15.380000,15.440000\n", b"")
        s = _run(tmp_path, {"VIX": dropped}, static_calendar)
        assert s.results["VIX"].status == "revised"
        root = tmp_path / "store" / "indices"
        (change,) = [json.loads(x) for x in (root / "changes.jsonl").read_text().splitlines()]
        assert change["date"] == "2026-09-17" and change["new"] is None
        assert (root / "VIX.2026-09-23.bak").exists()

    def test_a_shrunk_history_is_refused_and_the_store_kept(
        self, tmp_path: Path, static_calendar
    ) -> None:
        _run(tmp_path, {"VIX": VIX}, static_calendar)
        root = tmp_path / "store" / "indices"
        prior = (root / "VIX.csv").read_bytes()
        older_end = b"\n".join(VIX.split(b"\n")[:-2]) + b"\n"  # drops the 09-22 row
        s = _run(tmp_path, {"VIX": older_end}, static_calendar)
        assert s.results["VIX"].status == "invalid" and "shrunk" in s.results["VIX"].detail
        assert (root / "VIX.csv").read_bytes() == prior
        assert not list(root.glob("*.bak"))
        assert indices.exit_code(s) == 1
        # more than MAX_DROPPED past rows vanishing is refused too
        lines = VIX.split(b"\n")
        head_and_tail = b"\n".join([lines[0], lines[-2]]) + b"\n"
        s = _run(tmp_path, {"VIX": head_and_tail}, static_calendar)
        assert s.results["VIX"].status == "invalid"
        assert (root / "VIX.csv").read_bytes() == prior

    def test_lag_is_judged_in_sessions_per_source(self, tmp_path: Path, static_calendar) -> None:
        # 19:00 ET on 09-23: CBOE has not published 09-23 yet (its files were
        # last modified ~21:51 ET for 09-22), FRED trails by a session anyway
        s = _run(tmp_path, {"VIX": VIX, "DTB3": DTB3}, static_calendar, now=EVENING)
        assert s.results["VIX"].lagging and not s.results["DTB3"].lagging
        assert s.results["VIX"].status == "new"  # the history is still stored
        assert (tmp_path / "store/indices/VIX.csv").exists()
        assert indices.exit_code(s) == 3
        # two sessions behind is too much for FRED as well
        s = _run(
            tmp_path,
            {"DTB3": DTB3},
            static_calendar,
            now=datetime(2026, 9, 25, 7, 0, tzinfo=ET),  # expects 09-24
        )
        assert s.results["DTB3"].lagging

    def test_per_source_isolation_and_statuses(self, tmp_path: Path, static_calendar) -> None:
        bodies: dict[str, bytes | int | Exception] = {
            "VIX": VIX,
            "VVIX": 404,
            "SKEW": TimeoutError("read timed out"),
            "GVZ": b"<html>blocked</html>",
            "VXN": 503,
        }
        s = _run(tmp_path, bodies, static_calendar)
        got = {k: r.status for k, r in s.results.items()}
        assert got == {
            "VIX": "new",
            "VVIX": "missing",
            "SKEW": "error",
            "GVZ": "invalid",
            "VXN": "error",
        }
        assert "TimeoutError" in s.results["SKEW"].detail
        assert "503" in s.results["VXN"].detail
        assert (tmp_path / "store/indices/VIX.csv").exists()
        assert not (tmp_path / "store/indices/VVIX.csv").exists()
        assert {x["source"]: x["status"] for x in _prov(tmp_path)} == got
        assert indices.exit_code(s) == 1

    def test_dry_run_writes_nothing(self, tmp_path: Path, static_calendar) -> None:
        s = _run(tmp_path, {"VIX": VIX}, static_calendar, dry_run=True)
        assert s.results["VIX"].status == "new"
        assert not (tmp_path / "store").exists()

    def test_polite_pacing_and_headers(self, tmp_path: Path, static_calendar) -> None:
        calls: list[tuple[str, dict]] = []
        slept: list[float] = []
        _run(
            tmp_path,
            {"VIX": VIX, "VVIX": VVIX, "DTB3": DTB3},
            static_calendar,
            calls=calls,
            slept=slept,
        )
        assert slept == [indices.PACE_S, indices.PACE_S]
        by = {_key(u): h for u, h in calls}
        assert by["VIX"]["User-Agent"].startswith("Mozilla/5.0")
        # FRED's edge resets browser-looking clients (HTTP/2 INTERNAL_ERROR on
        # 2026-09-23); a plain tool UA over HTTP/1.1 is served
        assert not by["DTB3"]["User-Agent"].startswith("Mozilla")
        # and it stalls a request without an Accept header until the client
        # times out (2/2 probes, ~25 s) while "*/*" answers in 0.5 s (2/2)
        assert by["DTB3"]["Accept"] == "*/*"

    def test_transport_failures_are_soft_retryable_gaps(
        self, tmp_path: Path, static_calendar
    ) -> None:
        bodies: dict[str, bytes | int | Exception] = {
            "VIX": VIX,
            "DTB3": TimeoutError("The read operation timed out"),
            "SKEW": 503,
        }
        s = _run(tmp_path, bodies, static_calendar)
        assert {k: r.status for k, r in s.results.items()} == {
            "VIX": "new",
            "DTB3": "error",
            "SKEW": "error",
        }
        assert indices.exit_code(s) == 3  # the full history comes back next run
        gaps = [
            json.loads(x) for x in (tmp_path / "store/indices/gaps.jsonl").read_text().splitlines()
        ]
        # in SOURCES order (the CBOE files first)
        assert [(g["source"], g["status"]) for g in gaps] == [("SKEW", "error"), ("DTB3", "error")]
        assert "HTTP 503" in gaps[0]["detail"]
        assert "TimeoutError" in gaps[1]["detail"] and gaps[1]["at"] == MORNING.isoformat()
        # a later clean run adds no gap lines
        _run(tmp_path, {"DTB3": DTB3}, static_calendar)
        assert len((tmp_path / "store/indices/gaps.jsonl").read_text().splitlines()) == 2

    def test_exit_code_policy(self) -> None:
        def summary(*results: tuple[str, bool]) -> indices.IndicesSummary:
            return indices.IndicesSummary(
                date(2026, 9, 22),
                {
                    f"S{i}": indices.SourceResult(st, 1, "2026-09-22", lag, "", None)
                    for i, (st, lag) in enumerate(results)
                },
            )

        assert indices.exit_code(summary(("new", False), ("unchanged", False))) == 0
        assert indices.exit_code(summary(("updated", False), ("revised", False))) == 0
        assert indices.exit_code(summary(("unchanged", True), ("new", False))) == 3
        assert indices.exit_code(summary(("unchanged", True), ("error", False))) == 3
        assert indices.exit_code(summary(("new", False), ("error", False))) == 3
        assert indices.exit_code(summary(("error", False), ("missing", False))) == 1
        assert indices.exit_code(summary(("missing", False))) == 1
        assert indices.exit_code(summary(("invalid", False))) == 1
        assert indices.exit_code(summary(("error", False), ("error", False))) == 1  # nothing stored
        assert indices.exit_code(summary()) == 1


# ---------------------------------------------------------------------- CLI


class TestRecordIndicesCli:
    def test_summary_line_and_exit(
        self, tmp_path: Path, static_calendar, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = run_cli(
            ["record-indices", "--sources", "VIX,DTB3"],
            get=_get({"VIX": VIX, "DTB3": DTB3}),
            sleep=lambda _s: None,
            now=MORNING,
            cal=static_calendar,
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "record-indices session=2026-09-22 sources=2 new=2" in out
        assert "exit=0" in out
        assert (tmp_path / "store/indices/DTB3.csv").exists()

    def test_lagging_exit_3_and_bad_sources_exit_2(self, tmp_path: Path, static_calendar) -> None:
        kw = {"get": _get({"VIX": VIX}), "sleep": lambda _s: None, "cal": static_calendar}
        assert run_cli(["record-indices", "--sources", "VIX"], now=EVENING, **kw) == 3
        assert run_cli(["record-indices", "--sources", "VIX,NOPE"], now=MORNING, **kw) == 2
        assert run_cli(["record-indices", "--sources", ""], now=MORNING, **kw) == 2

    def test_dry_run_cli_writes_nothing(self, tmp_path: Path, static_calendar) -> None:
        rc = run_cli(
            ["record-indices", "--sources", "VIX", "--dry-run"],
            get=_get({"VIX": VIX}),
            sleep=lambda _s: None,
            now=MORNING,
            cal=static_calendar,
        )
        assert rc == 0 and not (tmp_path / "store").exists()
