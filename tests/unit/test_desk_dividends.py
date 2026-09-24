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

    def test_a_concurrent_recorder_s_snapshot_wins(self, store) -> None:
        # P2-11: another recorder publishes while this one is fetching
        target = store / "dividends" / "2026-09-23" / "AAPL.json"
        theirs = b'{"published": "first"}\n'

        class RacingWire(FakeWire):
            def __call__(self, url: str, *, timeout: float) -> HttpResponse:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(theirs)
                return super().__call__(url, timeout=timeout)

        run = dividends.record_dividends(
            SESSION,
            ["AAPL"],
            client=_client(RacingWire({"AAPL": _ok(AAPL)})),
            clock=lambda: datetime(2026, 9, 23, 19, 0, tzinfo=ET),
        )
        assert run.results["AAPL"].status == "exists"
        assert target.read_bytes() == theirs  # never replaced
        assert [p.name for p in target.parent.iterdir()] == ["AAPL.json"]  # no temp left
        assert run.exit_code == 0

    @pytest.mark.parametrize(
        "body",
        [
            {"status": "NOT_AUTHORIZED", "message": f"not entitled: /v3/x?apiKey={KEY}"},
            {"status": "ERROR", "message": f"bad request for apiKey={KEY}"},
        ],
    )
    def test_vendor_text_never_reaches_the_detail(
        self, body, store, static_calendar, capsys
    ) -> None:
        # P2-12: the client passes application-status messages through
        # unredacted; the recorder reports fixed codes only
        run = dividends.record_dividends(
            SESSION,
            ["AAPL"],
            client=_client(FakeWire({"AAPL": body})),
            clock=lambda: datetime(2026, 9, 23, 19, 0, tzinfo=ET),
        )
        detail = run.results["AAPL"].detail
        assert KEY not in detail and "apiKey" not in detail
        assert detail in {"not_entitled", "vendor_error"}
        rc = run_cli(
            ["record-dividends", "--session", "2026-09-23", "--symbols", "KO"],
            now=datetime(2026, 9, 23, 19, 0, tzinfo=ET),
            cal=static_calendar,
            dividend_client=_client(FakeWire({"KO": body})),
        )
        out = capsys.readouterr()
        assert rc == 1
        assert KEY not in out.out + out.err

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


def _ex(recs: list[dict], as_of: date, start: date, end: date):
    return dividends.ex_dividends(_snapshot(recs), as_of=as_of, start=start, end=end)


A0924 = date(2026, 9, 24)


class TestExDates:
    """A projection is the expected date last + k x period with the interval
    [expected - 7, expected + 7] days; it is returned when that interval
    overlaps the hold."""

    def test_projects_the_next_quarterly_ex_date_as_an_interval(self) -> None:
        # last declared 2026-08-10, quarterly: + 91 days = 2026-11-09,
        # interval [2026-11-02, 2026-11-16], the last regular amount 0.27
        got = _ex(AAPL, A0924, A0924, date(2026, 11, 20))
        assert got == (
            ExDividend(
                ex_date=date(2026, 11, 9),
                status="projected",
                earliest=date(2026, 11, 2),
                latest=date(2026, 11, 16),
                cash_amount=Decimal("0.27"),
                detail=got[0].detail,
            ),
        )
        assert "2026-08-10" in got[0].detail

    @pytest.mark.parametrize(
        ("start", "end", "hit"),
        [
            (A0924, date(2026, 11, 1), False),  # ends before the interval
            (A0924, date(2026, 11, 2), True),  # ends on its early edge
            (date(2026, 11, 3), date(2026, 11, 10), True),  # entered after the early edge
            (date(2026, 11, 16), date(2026, 11, 30), True),  # entered on its late edge
            (date(2026, 11, 17), date(2026, 11, 30), False),  # entered after it
        ],
    )
    def test_the_whole_interval_blocks(self, start, end, hit) -> None:
        got = _ex(AAPL, A0924, start, end)
        assert [x.ex_date for x in got] == ([date(2026, 11, 9)] if hit else [])

    def test_projection_repeats_through_a_long_window(self) -> None:
        # 2026-08-10 + 91k: k=1 2026-11-09, k=2 2027-02-08, k=3 2027-05-10
        # (interval from 2027-05-03, the window's last day)
        got = _ex(AAPL, A0924, A0924, date(2027, 5, 3))
        assert [(x.ex_date, x.status) for x in got] == [
            (date(2026, 11, 9), "projected"),
            (date(2027, 2, 8), "projected"),
            (date(2027, 5, 10), "projected"),
        ]

    def test_declared_future_date_is_used_and_projection_continues_from_it(self) -> None:
        recs = [*AAPL, _rec("AAPL", "2026-11-09", "2026-09-20")]
        got = _ex(recs, A0924, A0924, date(2027, 2, 5))
        # 2026-11-09 declared (a one-day interval); next: + 91 = 2027-02-08,
        # interval from 2027-02-01
        assert [(x.ex_date, x.status, x.earliest, x.latest) for x in got] == [
            (date(2026, 11, 9), "declared", date(2026, 11, 9), date(2026, 11, 9)),
            (date(2027, 2, 8), "projected", date(2027, 2, 1), date(2027, 2, 15)),
        ]

    def test_a_declaration_after_as_of_is_not_known_yet(self) -> None:
        recs = [*AAPL, _rec("AAPL", "2026-11-09", "2026-10-30")]
        got = _ex(recs, A0924, A0924, date(2026, 11, 20))
        assert [(x.ex_date, x.status) for x in got] == [(date(2026, 11, 9), "projected")]

    def test_special_dividends_are_declared_dates_but_never_projected(self) -> None:
        recs = [_rec("COST", "2026-10-05", "2026-09-15", amt="15", freq=0, kind="SC")]
        got = _ex(recs, A0924, A0924, date(2027, 6, 1))
        assert [(x.ex_date, x.status) for x in got] == [(date(2026, 10, 5), "declared")]
        assert got[0].cash_amount == Decimal("15")

    def test_a_special_dividend_never_suppresses_the_regular_projection(self) -> None:
        # P1-7: quarterly last 2026-08-10, a special declared for 2026-12-15:
        # the 2026-11-09 regular projection still stands
        recs = [*AAPL, _rec("AAPL", "2026-12-15", "2026-09-20", amt="1", freq=0, kind="SC")]
        got = _ex(recs, A0924, A0924, date(2026, 12, 31))
        assert [(x.ex_date, x.status) for x in got] == [
            (date(2026, 11, 9), "projected"),
            (date(2026, 12, 15), "declared"),
        ]

    def test_monthly_payer(self) -> None:
        # last 2026-09-01 monthly: + 30 = 2026-10-01 [09-24, 10-08], then
        # 2026-10-31 [10-24, 11-07]
        recs = [_rec("O", "2026-09-01", "2026-08-10", amt="0.26", freq=12)]
        got = _ex(recs, date(2026, 9, 20), date(2026, 9, 20), date(2026, 10, 24))
        assert [x.ex_date for x in got] == [date(2026, 10, 1), date(2026, 10, 31)]

    def test_non_payer_has_no_dates(self) -> None:
        assert _ex([], A0924, A0924, date(2027, 6, 1)) == ()

    @pytest.mark.parametrize(
        "recs",
        [
            [_rec("W", "2026-09-01", "2026-08-10", freq=52)],  # weekly: no period here
            [_rec("W", "2026-09-01", "2026-08-10", freq=None)],  # P1-6: frequency missing
            [_rec("W", "2026-09-01", "2026-08-10", freq=0)],  # a regular payment, no schedule
            [_rec("W", "2026-09-01", "2026-08-10", freq=None, kind=None)],  # type unknown
            # the latest regular record decides: an older quarterly doesn't vouch for it
            [
                _rec("W", "2026-06-01", "2026-05-10", freq=4),
                _rec("W", "2026-09-01", "2026-08-10", freq=None),
            ],
        ],
    )
    def test_unknown_schedule_fails_closed(self, recs) -> None:
        assert _ex(recs, A0924, A0924, date(2026, 12, 1)) is None

    def test_untyped_record_with_a_schedule_projects(self) -> None:
        recs = [_rec("W", "2026-08-10", "2026-07-30", freq=4, kind=None)]
        got = _ex(recs, A0924, A0924, date(2026, 11, 20))
        assert [x.ex_date for x in got] == [date(2026, 11, 9)]


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

    # -- P2-10: the document must be what its path says it is ---------------

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("symbol", "NFLX"),  # another symbol's (empty) history filed as AAPL
            ("session", "2026-09-15"),  # an old snapshot copied into a newer dir
            ("source", "somewhere else"),
            ("schema", "desk.dividends/0"),
            ("fetched_at", "2026-09-22T19:00:00-04:00"),  # fetched before its session
            ("fetched_at", "2026-09-23T19:00:00"),  # no zone: provenance unknown
            ("fetched_at", "yesterday"),
        ],
    )
    def test_identity_mismatch_is_unavailable(self, field, value, store, static_calendar) -> None:
        self._write(store, "2026-09-23", "AAPL", AAPL)
        path = store / "dividends" / "2026-09-23" / "AAPL.json"
        doc = json.loads(path.read_text())
        doc[field] = value
        path.write_text(json.dumps(doc))
        assert dividends.load_snapshot("AAPL", date(2026, 9, 24), static_calendar) is None

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
        assert [(x.ex_date, x.status) for x in got or ()] == [(date(2026, 11, 9), "projected")]
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
