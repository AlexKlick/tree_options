"""Ex-dividend dates for the ex-dividend rail (desk/dividends.py).

No network: the MassiveClient gets an injected transport. The response
shape is the one live request made 2026-09-23 (AAPL, Starter plan: status
OK, results with cash_amount/declaration_date/ex_dividend_date/frequency/
dividend_type). Every path is pinned to tmp; the fake key must never reach
a stored file. Projected dates are computed by hand in the comments.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tree_options.data.massive_client import (
    BackoffPolicy,
    HttpResponse,
    MassiveClient,
    MassiveTransportError,
    RateGovernor,
)
from tree_options.desk import dividends
from tree_options.desk.__main__ import run_cli
from tree_options.desk.rails import ExDividend

ET = ZoneInfo("America/New_York")
KEY = "FAKEKEY-dividends-0123456789"
SESSION = date(2026, 9, 23)


def _rec(ticker: str, ex: str, declared: str | None, *, amt="0.27", freq=4, kind="CD") -> dict:
    return {
        "cash_amount": float(amt) if "." in amt else int(amt),
        "currency": "USD",
        "declaration_date": declared,
        "dividend_type": kind,
        "ex_dividend_date": ex,
        "frequency": freq,
        "id": f"E{ticker}{ex}",
        "pay_date": ex,
        "record_date": ex,
        "ticker": ticker,
    }


AAPL = [
    _rec("AAPL", "2026-02-09", "2026-01-29", amt="0.26"),
    _rec("AAPL", "2026-05-11", "2026-04-30"),
    _rec("AAPL", "2026-08-10", "2026-07-30"),
]


class FakeWire:
    def __init__(self, bodies: dict[str, object]) -> None:
        self.bodies = bodies
        self.urls: list[str] = []

    def __call__(self, url: str, *, timeout: float) -> HttpResponse:
        self.urls.append(url)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        body = self.bodies.get(q["ticker"])
        if isinstance(body, Exception):
            raise body
        if body is None:
            body = {"status": "OK", "request_id": "r0", "results": []}
        return HttpResponse(200, json.dumps(body).encode())


def _client(wire: FakeWire) -> MassiveClient:
    return MassiveClient(
        api_key=KEY,
        transport=wire,
        cache_dir=None,
        governor=RateGovernor(None),
        backoff=BackoffPolicy(max_attempts=1),
    )


def _ok(results: list[dict]) -> dict:
    return {"status": "OK", "request_id": "req-1", "results": results}


@pytest.fixture()
def store(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "desk-store"
    monkeypatch.setenv("DESK_STORE", str(root))
    monkeypatch.setenv("TREX_DESK_STATE", str(tmp_path / "state"))
    return root


class TestRecord:
    def test_writes_one_normalized_snapshot_per_symbol(self, store) -> None:
        wire = FakeWire({"AAPL": _ok(AAPL)})
        run = dividends.record_dividends(
            SESSION,
            ["AAPL", "NFLX"],
            client=_client(wire),
            clock=lambda: datetime(2026, 9, 23, 19, 0, tzinfo=ET),
        )
        assert {s: r.status for s, r in run.results.items()} == {"AAPL": "ok", "NFLX": "ok"}
        assert run.exit_code == 0
        path = store / "dividends" / "2026-09-23" / "AAPL.json"
        raw = path.read_bytes()
        assert KEY.encode() not in raw
        doc = json.loads(raw)
        assert doc["symbol"] == "AAPL" and doc["session"] == "2026-09-23"
        assert doc["since"] == "2024-09-01"
        assert [r["ex_dividend_date"] for r in doc["records"]] == [
            "2026-02-09",
            "2026-05-11",
            "2026-08-10",
        ]
        assert doc["records"][0]["cash_amount"] == "0.26"  # money as a string, exact
        assert doc["records"][2]["frequency"] == 4
        # NFLX pays nothing: an empty, but recorded, history
        assert (
            json.loads((store / "dividends" / "2026-09-23" / "NFLX.json").read_text())["records"]
            == []
        )
        # one request per symbol, asking for that ticker from the history start
        assert len(wire.urls) == 2
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(wire.urls[0]).query))
        assert urllib.parse.urlsplit(wire.urls[0]).path == "/v3/reference/dividends"
        assert q["ticker"] == "AAPL"
        assert q["ex_dividend_date.gte"] == "2024-09-01"

    def test_rerun_is_idempotent_and_makes_no_request(self, store) -> None:
        wire = FakeWire({"AAPL": _ok(AAPL)})
        clock = lambda: datetime(2026, 9, 23, 19, 0, tzinfo=ET)  # noqa: E731
        dividends.record_dividends(SESSION, ["AAPL"], client=_client(wire), clock=clock)
        path = store / "dividends" / "2026-09-23" / "AAPL.json"
        first = path.read_bytes()
        wire2 = FakeWire({"AAPL": _ok(AAPL[:1])})  # a different answer would be ignored
        run = dividends.record_dividends(SESSION, ["AAPL"], client=_client(wire2), clock=clock)
        assert run.results["AAPL"].status == "exists"
        assert wire2.urls == []
        assert path.read_bytes() == first
        assert run.exit_code == 0

    def test_foreign_ticker_rows_are_dropped(self, store) -> None:
        wire = FakeWire({"AAPL": _ok([*AAPL, _rec("AAPLX", "2026-08-11", "2026-07-30")])})
        dividends.record_dividends(
            SESSION,
            ["AAPL"],
            client=_client(wire),
            clock=lambda: datetime(2026, 9, 23, 19, 0, tzinfo=ET),
        )
        doc = json.loads((store / "dividends" / "2026-09-23" / "AAPL.json").read_text())
        assert len(doc["records"]) == 3

    def test_transport_failure_is_a_soft_gap(self, store) -> None:
        wire = FakeWire({"AAPL": _ok(AAPL), "KO": MassiveTransportError("reset")})
        run = dividends.record_dividends(
            SESSION,
            ["AAPL", "KO"],
            client=_client(wire),
            clock=lambda: datetime(2026, 9, 23, 19, 0, tzinfo=ET),
        )
        assert run.results["KO"].status == "failed"
        assert KEY not in run.results["KO"].detail
        assert not (store / "dividends" / "2026-09-23" / "KO.json").exists()
        assert run.exit_code == 3

    def test_not_entitled_is_a_hard_failure(self, store) -> None:
        refusal = {"status": "NOT_AUTHORIZED", "message": "You are not entitled to this data."}
        wire = FakeWire({"AAPL": refusal})
        run = dividends.record_dividends(
            SESSION,
            ["AAPL"],
            client=_client(wire),
            clock=lambda: datetime(2026, 9, 23, 19, 0, tzinfo=ET),
        )
        assert run.results["AAPL"].status == "failed"
        assert run.exit_code == 1

    def test_dry_run_writes_nothing(self, store) -> None:
        wire = FakeWire({"AAPL": _ok(AAPL)})
        run = dividends.record_dividends(
            SESSION,
            ["AAPL"],
            client=_client(wire),
            clock=lambda: datetime(2026, 9, 23, 19, 0, tzinfo=ET),
            dry_run=True,
        )
        assert run.results["AAPL"].status == "dry-run"
        assert not (store / "dividends").exists()


def _snapshot(records: list[dict], session: str = "2026-09-23") -> dividends.DividendSnapshot:
    return dividends.snapshot_from_doc(
        {
            "schema": dividends.SCHEMA,
            "symbol": "AAPL",
            "session": session,
            "fetched_at": "2026-09-23T19:00:00-04:00",
            "source": dividends.SOURCE,
            "since": "2024-09-01",
            "request_ids": [],
            "records": [dividends.normalize_record(r) for r in records],
        }
    )


class TestExDates:
    def test_projects_the_next_quarterly_ex_date_early(self) -> None:
        # last declared 2026-08-10, quarterly: + 91 days = 2026-11-09,
        # projected 7 days early = 2026-11-02
        got = dividends.ex_dividends(
            _snapshot(AAPL),
            as_of=date(2026, 9, 24),
            start=date(2026, 9, 24),
            end=date(2026, 11, 20),
        )
        assert got == (ExDividend(date(2026, 11, 2), "projected", got[0].detail),)
        assert "2026-08-10" in got[0].detail

    def test_window_before_the_projection_is_clear(self) -> None:
        got = dividends.ex_dividends(
            _snapshot(AAPL), as_of=date(2026, 9, 24), start=date(2026, 9, 24), end=date(2026, 11, 1)
        )
        assert got == ()

    def test_projection_repeats_through_a_long_window(self) -> None:
        # 2026-08-10 + 91k - 7: k=1 2026-11-02, k=2 2027-02-01, k=3 2027-05-03
        got = dividends.ex_dividends(
            _snapshot(AAPL), as_of=date(2026, 9, 24), start=date(2026, 9, 24), end=date(2027, 5, 3)
        )
        assert [(x.ex_date, x.status) for x in got] == [
            (date(2026, 11, 2), "projected"),
            (date(2027, 2, 1), "projected"),
            (date(2027, 5, 3), "projected"),
        ]

    def test_declared_future_date_is_used_and_projection_continues_from_it(self) -> None:
        recs = [*AAPL, _rec("AAPL", "2026-11-09", "2026-09-20")]
        got = dividends.ex_dividends(
            _snapshot(recs), as_of=date(2026, 9, 24), start=date(2026, 9, 24), end=date(2027, 2, 5)
        )
        # 2026-11-09 declared; next: 2026-11-09 + 91 - 7 = 2027-02-01
        assert [(x.ex_date, x.status) for x in got] == [
            (date(2026, 11, 9), "declared"),
            (date(2027, 2, 1), "projected"),
        ]

    def test_a_declaration_after_as_of_is_not_known_yet(self) -> None:
        recs = [*AAPL, _rec("AAPL", "2026-11-09", "2026-10-30")]
        got = dividends.ex_dividends(
            _snapshot(recs),
            as_of=date(2026, 9, 24),
            start=date(2026, 9, 24),
            end=date(2026, 11, 20),
        )
        assert [(x.ex_date, x.status) for x in got] == [(date(2026, 11, 2), "projected")]

    def test_special_dividends_are_declared_dates_but_never_projected(self) -> None:
        recs = [_rec("COST", "2026-10-05", "2026-09-15", amt="15", freq=0, kind="SC")]
        got = dividends.ex_dividends(
            _snapshot(recs), as_of=date(2026, 9, 24), start=date(2026, 9, 24), end=date(2027, 6, 1)
        )
        assert [(x.ex_date, x.status) for x in got] == [(date(2026, 10, 5), "declared")]

    def test_monthly_payer(self) -> None:
        # last 2026-09-01 monthly: + 30 - 7 -> 2026-09-24, then 2026-10-24
        recs = [_rec("O", "2026-09-01", "2026-08-10", amt="0.26", freq=12)]
        got = dividends.ex_dividends(
            _snapshot(recs),
            as_of=date(2026, 9, 20),
            start=date(2026, 9, 20),
            end=date(2026, 10, 24),
        )
        assert [x.ex_date for x in got] == [date(2026, 9, 24), date(2026, 10, 24)]

    def test_non_payer_has_no_dates(self) -> None:
        got = dividends.ex_dividends(
            _snapshot([]), as_of=date(2026, 9, 24), start=date(2026, 9, 24), end=date(2027, 6, 1)
        )
        assert got == ()

    def test_unknown_schedule_fails_closed(self) -> None:
        recs = [_rec("W", "2026-09-01", "2026-08-10", freq=52)]
        assert (
            dividends.ex_dividends(
                _snapshot(recs),
                as_of=date(2026, 9, 24),
                start=date(2026, 9, 24),
                end=date(2026, 12, 1),
            )
            is None
        )


class TestLoad:
    def _write(self, store: Path, session: str, sym: str, records: list[dict]) -> None:
        d = store / "dividends" / session
        d.mkdir(parents=True, exist_ok=True)
        doc = {
            "schema": dividends.SCHEMA,
            "symbol": sym,
            "session": session,
            "fetched_at": f"{session}T19:00:00-04:00",
            "source": dividends.SOURCE,
            "since": "2024-09-01",
            "request_ids": [],
            "records": [dividends.normalize_record(r) for r in records],
        }
        (d / f"{sym}.json").write_text(json.dumps(doc))

    def test_latest_snapshot_on_or_before_as_of(self, store, static_calendar) -> None:
        self._write(store, "2026-09-21", "AAPL", AAPL[:1])
        self._write(store, "2026-09-23", "AAPL", AAPL)
        self._write(store, "2026-09-25", "AAPL", [])  # after as_of: invisible
        snap = dividends.load_snapshot("AAPL", date(2026, 9, 24), static_calendar)
        assert snap is not None and snap.session == date(2026, 9, 23)
        assert len(snap.records) == 3

    def test_stale_snapshot_is_unavailable(self, store, static_calendar) -> None:
        # 2026-09-15 -> 2026-09-24 is 7 sessions > 5
        self._write(store, "2026-09-15", "AAPL", AAPL)
        assert dividends.load_snapshot("AAPL", date(2026, 9, 24), static_calendar) is None
        # 2026-09-17 -> 2026-09-24 is 5 sessions: fresh enough
        self._write(store, "2026-09-17", "AAPL", AAPL)
        assert dividends.load_snapshot("AAPL", date(2026, 9, 24), static_calendar) is not None

    def test_no_snapshot_is_unavailable(self, store, static_calendar) -> None:
        assert dividends.load_snapshot("AAPL", date(2026, 9, 24), static_calendar) is None
        assert (
            dividends.ex_dividends_for(
                "AAPL",
                date(2026, 9, 24),
                date(2026, 10, 22),
                static_calendar,
                as_of=date(2026, 9, 24),
            )
            is None
        )

    def test_corrupt_snapshot_is_unavailable(self, store, static_calendar) -> None:
        d = store / "dividends" / "2026-09-23"
        d.mkdir(parents=True)
        (d / "AAPL.json").write_text("{torn")
        assert dividends.load_snapshot("AAPL", date(2026, 9, 24), static_calendar) is None

    def test_end_to_end_for_the_rail(self, store, static_calendar) -> None:
        self._write(store, "2026-09-23", "AAPL", AAPL)
        self._write(store, "2026-09-23", "NFLX", [])
        got = dividends.ex_dividends_for(
            "AAPL", date(2026, 9, 24), date(2026, 11, 20), static_calendar, as_of=date(2026, 9, 24)
        )
        assert [(x.ex_date, x.status) for x in got or ()] == [(date(2026, 11, 2), "projected")]
        assert (
            dividends.ex_dividends_for(
                "NFLX",
                date(2026, 9, 24),
                date(2026, 11, 20),
                static_calendar,
                as_of=date(2026, 9, 24),
            )
            == ()
        )


class TestCli:
    def test_record_dividends_command(self, store, static_calendar) -> None:
        wire = FakeWire({"AAPL": _ok(AAPL)})
        rc = run_cli(
            ["record-dividends", "--session", "2026-09-23", "--symbols", "AAPL,KO"],
            now=datetime(2026, 9, 23, 19, 0, tzinfo=ET),
            cal=static_calendar,
            dividend_client=_client(wire),
        )
        assert rc == 0
        assert sorted(p.name for p in (store / "dividends" / "2026-09-23").iterdir()) == [
            "AAPL.json",
            "KO.json",
        ]

    def test_default_session_is_the_latest_completed(self, store, static_calendar) -> None:
        wire = FakeWire({})
        rc = run_cli(
            ["record-dividends", "--symbols", "AAPL"],
            now=datetime(2026, 9, 24, 10, 0, tzinfo=ET),
            cal=static_calendar,
            dividend_client=_client(wire),
        )
        assert rc == 0
        assert (store / "dividends" / "2026-09-23" / "AAPL.json").exists()

    @pytest.mark.parametrize(
        "argv",
        [
            ["record-dividends", "--symbols", "aapl;rm"],
            ["record-dividends", "--session", "2026-09-26"],  # a Saturday
        ],
    )
    def test_bad_arguments(self, argv, store, static_calendar) -> None:
        rc = run_cli(
            argv,
            now=datetime(2026, 9, 28, 10, 0, tzinfo=ET),
            cal=static_calendar,
            dividend_client=_client(FakeWire({})),
        )
        assert rc == 2

    def test_dry_run(self, store, static_calendar) -> None:
        rc = run_cli(
            ["record-dividends", "--session", "2026-09-23", "--symbols", "AAPL", "--dry-run"],
            now=datetime(2026, 9, 23, 19, 0, tzinfo=ET),
            cal=static_calendar,
            dividend_client=_client(FakeWire({"AAPL": _ok(AAPL)})),
        )
        assert rc == 0
        assert not (store / "dividends").exists()


def test_money_is_never_a_float() -> None:
    rec = dividends.normalize_record(_rec("AAPL", "2026-08-10", "2026-07-30", amt="0.2675"))
    assert rec["cash_amount"] == "0.2675"
    assert Decimal(rec["cash_amount"]) == Decimal("0.2675")
