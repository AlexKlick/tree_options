"""Desk D3 events: the sealed macro calendar (FOMC parse, OpEx and VIX
expiry, seal), earnings timing (Nasdaq estimated, EDGAR 8-K 2.02
confirmed), the merged readers, and the ``update-events`` /
``seal-macro`` CLIs.

No network: every fetch goes through an injected ``get``. The Fed page
and Nasdaq fixtures are trimmed captures of one real request each
(2026-09-23); the SEC payloads are synthetic (tests/fixtures/desk_sec.py).
Every path is pinned to tmp.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.fixtures import desk_sec
from tree_options.desk import events
from tree_options.desk.__main__ import run_cli
from tree_options.time.calendar import StaticSessionCalendar

ET = ZoneInfo("America/New_York")
REPO = Path(__file__).resolve().parents[2]
FIX = REPO / "tests" / "fixtures" / "desk_events"
FOMC_HTML = (FIX / "fomccalendars_trimmed.html").read_bytes()
NASDAQ_0924 = (FIX / "nasdaq_earnings_2026-09-24.json").read_bytes()
SATURDAY = datetime(2026, 9, 26, 10, 0, tzinfo=ET)
UA = "desk-test-agent contact-redacted"

# FOMC decision days on the Fed page (2026 and 2027 panels), by hand
FOMC_2026 = [
    "2026-01-28",
    "2026-03-18",
    "2026-04-29",
    "2026-06-17",
    "2026-07-29",
    "2026-09-16",
    "2026-10-28",
    "2026-12-09",
]
FOMC_2027 = [
    "2027-01-27",
    "2027-03-17",
    "2027-04-28",
    "2027-06-09",
    "2027-07-28",
    "2027-09-15",
    "2027-10-27",
    "2027-12-08",
]
SEP_MONTHS = {3, 6, 9, 12}
# third Fridays of 2026, June moved to Thursday (Juneteenth 06-19 is a holiday)
OPEX_2026 = [
    "2026-01-16",
    "2026-02-20",
    "2026-03-20",
    "2026-04-17",
    "2026-05-15",
    "2026-06-18",
    "2026-07-17",
    "2026-08-21",
    "2026-09-18",
    "2026-10-16",
    "2026-11-20",
    "2026-12-18",
]
# Cboe VIX final settlement dates for 2025 (March: Tuesday, because the
# April 2025 third Friday was Good Friday)
VIX_2025 = [
    "2025-01-22",
    "2025-02-19",
    "2025-03-18",
    "2025-04-16",
    "2025-05-21",
    "2025-06-18",
    "2025-07-16",
    "2025-08-20",
    "2025-09-17",
    "2025-10-22",
    "2025-11-19",
    "2025-12-17",
]


@pytest.fixture()
def trex_calendar() -> StaticSessionCalendar:
    base = REPO / "data" / "calendar" / "trex" / "nyse_sessions_2018_01_02_2028_12_29.json"
    return StaticSessionCalendar(base, base.with_suffix(".sha256"))


@pytest.fixture(autouse=True)
def _pinned_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DESK_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("TREX_DESK_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("DESK_PAPER_DIR", str(tmp_path / "paper"))
    monkeypatch.setenv("DESK_EVENTS_DIR", str(tmp_path / "events"))
    monkeypatch.setenv("TREX_NOTIFY_ENV", str(tmp_path / "no-notify.env"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DESK_REPO_ROOT", raising=False)
    monkeypatch.delenv("DESK_SEC_UA", raising=False)


def _paper(tmp_path: Path, calendar: dict[str, list[str]] | None = None) -> Path:
    paper = tmp_path / "paper"
    paper.mkdir(parents=True, exist_ok=True)
    cal = (
        calendar
        if calendar is not None
        else {
            "AAPL": ["2026-07-30", "2026-10-29"],
            "COST": ["2026-09-24"],
            "XOM": ["2026-10-30"],
            "SPY": [],
        }
    )
    (paper / "earnings-calendar.json").write_text(json.dumps(cal))
    return paper


def _nasdaq(rows: list[tuple[str, str]] | None) -> bytes:
    data = (
        None
        if rows is None
        else {
            "asOf": "x",
            "headers": {},
            "rows": [{"symbol": s, "time": t, "name": s} for s, t in rows],
        }
    )
    return json.dumps({"data": data, "message": None, "status": {"rCode": 200}}).encode()


class Web:
    """A fake web: url -> (status, body); records every request."""

    def __init__(self, routes: dict[str, bytes | int | Exception]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, *, headers, timeout: float) -> tuple[int, bytes]:
        self.calls.append((url, dict(headers)))
        for key, body in self.routes.items():
            if key in url:
                if isinstance(body, Exception):
                    raise body
                if isinstance(body, int):
                    return body, b""
                return 200, body
        return 404, b""


# ----------------------------------------------------------------- FOMC


class TestFomc:
    def test_parses_the_2026_and_2027_panels(self) -> None:
        ms = events.parse_fomc(FOMC_HTML.decode())
        sched = [m for m in ms if m.kind == "scheduled"]
        got = sorted(m.end.isoformat() for m in sched if m.end.year in (2026, 2027))
        assert got == FOMC_2026 + FOMC_2027
        for m in sched:
            if m.end.year in (2026, 2027):
                assert m.sep == (m.end.month in SEP_MONTHS)
                # two-day meetings: start is the calendar day before
                assert (m.start.year, m.start.month, m.start.day + 1) == (
                    m.end.year,
                    m.end.month,
                    m.end.day,
                )

    def test_cross_month_meetings_and_notation_votes(self) -> None:
        ms = {m.end: m for m in events.parse_fomc(FOMC_HTML.decode())}
        m = ms[date(2024, 5, 1)]
        assert m.start == date(2024, 4, 30) and m.kind == "scheduled"
        assert ms[date(2023, 2, 1)].start == date(2023, 1, 31)
        assert ms[date(2023, 11, 1)].start == date(2023, 10, 31)
        vote = ms[date(2025, 8, 22)]
        assert vote.kind == "notation_vote" and vote.start == vote.end and not vote.sep

    @pytest.mark.parametrize(
        "html",
        [
            "",
            "<html><body>Service unavailable</body></html>",
            '<h4><a id="1">2026 FOMC Meetings</a></h4>',  # a panel with no rows
            '<h4><a id="1">2026 FOMC Meetings</a></h4><div class="fomc-meeting__month">'
            '<strong>January</strong></div><div class="fomc-meeting__date">TBD</div>',
            '<h4><a id="1">2026 FOMC Meetings</a></h4><div class="fomc-meeting__month">'
            '<strong>Smarch</strong></div><div class="fomc-meeting__date">1-2</div>',
        ],
    )
    def test_unreadable_pages_raise_never_empty(self, html: str) -> None:
        with pytest.raises(events.FomcParseError):
            events.parse_fomc(html)


# -------------------------------------------------------- OpEx / VIX expiry


class TestExpiries:
    def test_monthly_opex_2026(self, trex_calendar) -> None:
        got = [events.monthly_opex(2026, m, trex_calendar).isoformat() for m in range(1, 13)]
        assert got == OPEX_2026

    def test_vix_expiry_2025_matches_cboe(self, trex_calendar) -> None:
        got = [events.vix_expiry(2025, m, trex_calendar).isoformat() for m in range(1, 13)]
        assert got == VIX_2025

    def test_vix_expiry_holiday_rules(self, trex_calendar) -> None:
        # June 2026 opex Friday 06-19 is Juneteenth: 30 days before Thu 06-18
        assert events.vix_expiry(2026, 5, trex_calendar) == date(2026, 5, 19)
        # June 2024: the Wednesday (06-19, Juneteenth) is a holiday -> Tuesday
        assert events.vix_expiry(2024, 6, trex_calendar) == date(2024, 6, 18)
        # December rolls into January of the next year
        assert events.vix_expiry(2026, 12, trex_calendar) == date(2026, 12, 16)

    def test_past_the_calendar_raises(self, static_calendar) -> None:
        # the protocol calendar ends 2026-12-31: never guess past it
        with pytest.raises(events.EventsError):
            events.monthly_opex(2027, 1, static_calendar)
        with pytest.raises(events.EventsError):
            events.vix_expiry(2026, 12, static_calendar)


# ---------------------------------------------------------- macro seal


class TestMacro:
    def _build(self, cal, **kw) -> dict:
        return events.build_macro(
            events.parse_fomc(FOMC_HTML.decode()),
            cal,
            date(2026, 1, 1),
            date(2027, 12, 31),
            fetched_on=date(2026, 9, 23),
            **kw,
        )

    def test_build_never_fabricates_cpi_or_nfp(self, trex_calendar) -> None:
        doc = self._build(trex_calendar)
        assert doc["schema"] == "desk-macro/1"
        assert [f["date"] for f in doc["fomc"]] == FOMC_2026 + FOMC_2027
        assert doc["cpi"] == [] and doc["nfp"] == []
        assert any("cpi" in t and "operator/agent entry required" in t for t in doc["todo"])
        assert any("nfp" in t and "operator/agent entry required" in t for t in doc["todo"])
        assert doc["opex"][:12] == OPEX_2026 and len(doc["opex"]) == 24
        assert len(doc["vix_expiry"]) == 24 and doc["vix_expiry"][4] == "2026-05-19"
        assert doc["fomc_source"]["url"] == events.FOMC_URL

    def test_hand_entered_items_are_carried_and_validated(self, trex_calendar) -> None:
        item = {"date": "2026-10-14", "source": "bls.gov schedule", "entered_by": "operator"}
        doc = self._build(trex_calendar, cpi=[item])
        assert doc["cpi"] == [item]
        assert not any("cpi" in t for t in doc["todo"])
        with pytest.raises(events.EventsError):
            self._build(
                trex_calendar, cpi=[{"date": "2026-10-14", "source": "", "entered_by": "x"}]
            )
        with pytest.raises(events.EventsError):
            self._build(
                trex_calendar, nfp=[{"date": "10/02/2026", "source": "s", "entered_by": "x"}]
            )

    def test_seal_load_and_tamper(self, tmp_path: Path, trex_calendar) -> None:
        doc = self._build(trex_calendar)
        path = tmp_path / "events" / "macro-2026-2027.json"
        sha = events.seal_macro(doc, path, basis="test")
        data = path.read_bytes()
        assert sha == hashlib.sha256(data).hexdigest()
        assert path.with_suffix(".sha256").read_text().split()[0] == sha
        assert events.load_macro(path) == doc
        seals = (path.parent / "MACRO-SEALS.md").read_text()
        assert sha in seals and "| test |" in seals
        path.write_bytes(data.replace(b"2026-09-16", b"2026-09-17"))
        with pytest.raises(events.MacroSealError):
            events.load_macro(path)

    def test_macro_events_in_range_and_fail_closed_outside(
        self, tmp_path: Path, trex_calendar
    ) -> None:
        path = tmp_path / "events" / "macro-2026-2027.json"
        events.seal_macro(self._build(trex_calendar), path, basis="test")
        got = events.macro_events(date(2026, 9, 14), date(2026, 9, 21), path=path)
        assert [(e.date.isoformat(), e.kind) for e in got] == [
            ("2026-09-16", "fomc"),
            ("2026-09-16", "vix_expiry"),
            ("2026-09-18", "opex"),
        ]
        assert got[0].detail == "SEP"
        with pytest.raises(events.EventsError):
            events.macro_events(date(2027, 12, 1), date(2028, 1, 31), path=path)
        assert events.macro_gaps(path=path) == ["cpi", "nfp"]

    def test_the_committed_macro_file(self, trex_calendar) -> None:
        path = REPO / "data" / "desk" / "events" / "macro-2026-2027.json"
        doc = events.load_macro(path)  # the seal verifies
        assert [f["date"] for f in doc["fomc"]] == FOMC_2026 + FOMC_2027
        assert all(f["sep"] == (int(f["date"][5:7]) in SEP_MONTHS) for f in doc["fomc"])
        assert doc["opex"][:12] == OPEX_2026
        for item in doc["cpi"] + doc["nfp"]:  # hand entries carry provenance
            assert item["source"] and item["entered_by"]
        assert "MACRO-SEALS.md" in {p.name for p in path.parent.iterdir()}
        seals = (path.parent / "MACRO-SEALS.md").read_text()
        assert path.with_suffix(".sha256").read_text().split()[0] in seals


# ------------------------------------------------------- vendor parsers


class TestVendorParsers:
    def test_nasdaq_timing_codes(self) -> None:
        assert events.parse_nasdaq(NASDAQ_0924) == {
            "COST": ("amc", "time-after-hours"),
            "DRI": ("bmo", "time-pre-market"),
            "VFS": ("unknown", "time-not-supplied"),
        }

    def test_nasdaq_empty_or_null_is_empty_never_default(self) -> None:
        assert events.parse_nasdaq(_nasdaq(None)) == {}
        assert events.parse_nasdaq(_nasdaq([])) == {}
        with pytest.raises(events.VendorParseError):
            events.parse_nasdaq(b"<html>blocked</html>")
        with pytest.raises(events.VendorParseError):
            events.parse_nasdaq(b'{"data": {"rows": "x"}}')

    @pytest.mark.parametrize(
        ("raw", "want"),
        [
            ("2026-07-30T16:30:12.000Z", ("2026-07-30", "amc")),
            ("2026-04-30T07:00:05.000Z", ("2026-04-30", "bmo")),
            ("2026-04-30T09:29:59.000Z", ("2026-04-30", "bmo")),
            ("2026-04-30T09:30:00.000Z", ("2026-04-30", "unknown")),
            ("2026-01-29T12:00:00.000Z", ("2026-01-29", "unknown")),
            ("2025-11-06T16:00:00.000Z", ("2025-11-06", "amc")),
            ("2026-04-30T05:59:00.000Z", ("2026-04-30", "unknown")),  # EDGAR closed
            ("2026-04-30T23:10:00.000Z", ("2026-04-30", "unknown")),  # EDGAR closed
            ("2026-09-26T17:00:00.000Z", ("2026-09-26", "unknown")),  # a Saturday
        ],
    )
    def test_acceptance_timing_reads_edgar_digits_as_eastern(
        self, raw: str, want: tuple[str, str], trex_calendar
    ) -> None:
        d, timing, _note = events.acceptance_timing(raw, trex_calendar)
        assert (d.isoformat(), timing) == want

    def test_submissions_keep_only_8k_item_202_since_2021(self, trex_calendar) -> None:
        filings, pages = events.parse_submissions(
            desk_sec.submissions_payload(
                desk_sec.AAPL_FILINGS,
                files=[
                    {"name": "CIK0000320193-submissions-001.json", "filingTo": "2022-03-01"},
                    {"name": "CIK0000320193-submissions-002.json", "filingTo": "2014-12-30"},
                ],
            )
        )
        assert sorted(f.accepted for f in filings) == [
            "2025-11-06T16:00:00.000Z",
            "2026-01-29T12:00:00.000Z",
            "2026-04-30T07:00:05.000Z",
            "2026-07-30T16:30:12.000Z",
        ]
        assert pages == ["CIK0000320193-submissions-001.json"]

    def test_company_tickers_map(self) -> None:
        got = events.parse_company_tickers(desk_sec.tickers_payload(), ["AAPL", "BRK.B", "ZZZZ"])
        assert got == {"AAPL": "0000320193", "BRK.B": "0001067983"}


# ------------------------------------------------------- update-events


def _routes(**extra: bytes | int | Exception) -> dict[str, bytes | int | Exception]:
    routes: dict[str, bytes | int | Exception] = {
        "date=2026-09-28": _nasdaq([("AAPL", "time-pre-market")]),
        "date=2026-09-29": _nasdaq([("XOM", "time-after-hours"), ("ZZZZ", "time-pre-market")]),
        "date=2026-09-30": _nasdaq([]),
        "fomccalendars": FOMC_HTML,
    }
    routes.update(extra)
    return routes


def _update(tmp_path: Path, web: Web, cal, *, horizon: int = 3, now: datetime = SATURDAY):
    return events.update_events(
        get=web,
        clock=lambda: now,
        sleep=lambda _s: None,
        cal=cal,
        paper=tmp_path / "paper",
        events_dir=tmp_path / "events",
        state=tmp_path / "state",
        sec_ua=None,
        horizon=horizon,
    )


def _seal(tmp_path: Path, cal) -> Path:
    doc = events.build_macro(
        events.parse_fomc(FOMC_HTML.decode()),
        cal,
        date(2026, 1, 1),
        date(2027, 12, 31),
        fetched_on=date(2026, 9, 23),
    )
    path = tmp_path / "events" / "macro-2026-2027.json"
    events.seal_macro(doc, path, basis="test")
    return path


class TestUpdateEvents:
    def test_estimated_timing_from_nasdaq_and_ua_missing_skips_sec(
        self, tmp_path: Path, trex_calendar
    ) -> None:
        paper = _paper(tmp_path)
        sealed_before = (paper / "earnings-calendar.json").read_bytes()
        _seal(tmp_path, trex_calendar)
        web = Web(_routes())
        res = _update(tmp_path, web, trex_calendar)
        assert not any("sec.gov" in u for u, _h in web.calls)
        assert res.edgar == "skipped" and "sec_ua_missing" in res.notes
        doc = json.loads((paper / "earnings-timing.json").read_text())
        assert doc == {
            "AAPL": {
                "2026-09-28": {
                    "timing": "bmo",
                    "source": "nasdaq earnings calendar (time-pre-market)",
                    "fetched_at": SATURDAY.isoformat(),
                    "status": "estimated",
                }
            },
            "XOM": {
                "2026-09-29": {
                    "timing": "amc",
                    "source": "nasdaq earnings calendar (time-after-hours)",
                    "fetched_at": SATURDAY.isoformat(),
                    "status": "estimated",
                }
            },
        }
        assert (paper / "earnings-calendar.json").read_bytes() == sealed_before
        assert res.macro == "ok" and res.exit_code == 0
        assert "sec_ua_missing" in res.line()

    def test_rerun_is_byte_identical(self, tmp_path: Path, trex_calendar) -> None:
        paper = _paper(tmp_path)
        _seal(tmp_path, trex_calendar)
        _update(tmp_path, Web(_routes()), trex_calendar)
        first = (paper / "earnings-timing.json").read_bytes()
        later = datetime(2026, 9, 26, 11, 0, tzinfo=ET)
        _update(tmp_path, Web(_routes()), trex_calendar, now=later)
        assert (paper / "earnings-timing.json").read_bytes() == first

    def test_moved_estimate_is_dropped_but_an_empty_day_drops_nothing(
        self, tmp_path: Path, trex_calendar
    ) -> None:
        paper = _paper(tmp_path)
        _seal(tmp_path, trex_calendar)
        _update(tmp_path, Web(_routes()), trex_calendar)
        # next week the vendor lists XOM on 09-30 instead, and 09-28 comes back empty
        moved = _routes(
            **{
                "date=2026-09-28": _nasdaq([]),
                "date=2026-09-29": _nasdaq([("KO", "time-pre-market")]),
                "date=2026-09-30": _nasdaq([("XOM", "time-not-supplied")]),
            }
        )
        _update(tmp_path, Web(moved), trex_calendar)
        doc = json.loads((paper / "earnings-timing.json").read_text())
        assert "2026-09-28" in doc["AAPL"]  # an empty answer is no evidence
        assert set(doc["XOM"]) == {"2026-09-30"}  # moved: the old date is gone
        assert doc["XOM"]["2026-09-30"]["timing"] == "unknown"

    def test_edgar_confirmed_with_the_ua_from_env_only(self, tmp_path: Path, trex_calendar) -> None:
        paper = _paper(tmp_path)
        _seal(tmp_path, trex_calendar)
        page = desk_sec.page_payload([("8-K", "2.02,9.01", "2021-04-28T16:30:00.000Z")])
        web = Web(
            _routes(
                **{
                    "company_tickers.json": desk_sec.tickers_payload(),
                    "CIK0000320193.json": desk_sec.submissions_payload(
                        desk_sec.AAPL_FILINGS,
                        files=[
                            {"name": "CIK0000320193-submissions-001.json", "filingTo": "2022-01-01"}
                        ],
                    ),
                    "CIK0000320193-submissions-001.json": page,
                    "CIK0000034088.json": desk_sec.submissions_payload([]),
                }
            )
        )
        res = events.update_events(
            get=web,
            clock=lambda: SATURDAY,
            sleep=lambda _s: None,
            cal=trex_calendar,
            paper=paper,
            events_dir=tmp_path / "events",
            state=tmp_path / "state",
            sec_ua=UA,
            horizon=3,
            names=("AAPL", "XOM"),
        )
        sec_calls = [(u, h) for u, h in web.calls if "sec.gov" in u]
        assert sec_calls and all(h["User-Agent"] == UA for _u, h in sec_calls)
        assert not any(h.get("User-Agent") == UA for u, h in web.calls if "sec.gov" not in u)
        doc = json.loads((paper / "earnings-timing.json").read_text())
        aapl = doc["AAPL"]
        assert aapl["2026-07-30"]["status"] == "confirmed" and aapl["2026-07-30"]["timing"] == "amc"
        assert aapl["2026-04-30"]["timing"] == "bmo"
        assert aapl["2026-01-29"]["timing"] == "unknown"
        assert aapl["2021-04-28"]["timing"] == "amc"  # from the older page
        assert "2020-10-29" not in aapl
        assert "8-K 2.02" in aapl["2026-07-30"]["source"]
        assert aapl["2026-09-28"]["status"] == "estimated"
        assert res.edgar == "ok" and res.confirmed == 5
        assert UA not in (paper / "earnings-timing.json").read_text()
        assert UA not in res.line()

    def test_confirmed_is_never_downgraded_by_an_estimate(
        self, tmp_path: Path, trex_calendar
    ) -> None:
        paper = _paper(tmp_path)
        _seal(tmp_path, trex_calendar)
        (paper / "earnings-timing.json").write_text(
            json.dumps(
                {
                    "AAPL": {
                        "2026-09-28": {
                            "timing": "amc",
                            "source": "sec-edgar 8-K 2.02 x",
                            "fetched_at": "2026-09-01T00:00:00+00:00",
                            "status": "confirmed",
                        }
                    }
                }
            )
        )
        _update(tmp_path, Web(_routes()), trex_calendar)
        doc = json.loads((paper / "earnings-timing.json").read_text())
        assert doc["AAPL"]["2026-09-28"]["status"] == "confirmed"
        assert doc["AAPL"]["2026-09-28"]["timing"] == "amc"

    def test_macro_drift_and_broken_seal_exit_1(self, tmp_path: Path, trex_calendar) -> None:
        _paper(tmp_path)
        _seal(tmp_path, trex_calendar)
        drifted = FOMC_HTML.replace(b">15-16*<", b">22-23*<")
        res = _update(tmp_path, Web(_routes(fomccalendars=drifted)), trex_calendar)
        assert res.macro == "drift" and res.exit_code == 1
        drift = json.loads((tmp_path / "state" / "events" / "macro-drift.json").read_text())
        assert drift["removed"][0]["date"] == "2026-09-16"
        assert drift["added"][0]["date"] == "2026-09-23"
        path = tmp_path / "events" / "macro-2026-2027.json"
        path.write_bytes(path.read_bytes().replace(b"2026-10-28", b"2026-10-29"))
        res = _update(tmp_path, Web(_routes()), trex_calendar)
        assert res.macro == "seal_broken" and res.exit_code == 1

    def test_partial_and_total_vendor_failure(self, tmp_path: Path, trex_calendar) -> None:
        _paper(tmp_path)
        _seal(tmp_path, trex_calendar)
        res = _update(tmp_path, Web(_routes(**{"date=2026-09-29": 503})), trex_calendar)
        assert res.exit_code == 3 and res.nasdaq_ok == 2
        res = _update(
            tmp_path,
            Web(
                _routes(
                    **{
                        "date=2026-09-28": TimeoutError("t"),
                        "date=2026-09-29": 403,
                        "date=2026-09-30": b"<html>",
                    }
                )
            ),
            trex_calendar,
        )
        assert res.exit_code == 1 and res.nasdaq_ok == 0

    def test_fed_page_unreachable_is_retryable(self, tmp_path: Path, trex_calendar) -> None:
        _paper(tmp_path)
        _seal(tmp_path, trex_calendar)
        res = _update(tmp_path, Web(_routes(fomccalendars=503)), trex_calendar)
        assert res.macro == "unverified" and res.exit_code == 3


# ------------------------------------------------------------ readers


class TestReaders:
    def _timing(self, paper: Path) -> None:
        (paper / "earnings-timing.json").write_text(
            json.dumps(
                {
                    "AAPL": {
                        "2026-10-29": {
                            "timing": "amc",
                            "source": "nasdaq earnings calendar (time-after-hours)",
                            "fetched_at": "t",
                            "status": "estimated",
                        },
                        "2026-10-28": {
                            "timing": "unknown",
                            "source": "nasdaq earnings calendar (time-not-supplied)",
                            "fetched_at": "t",
                            "status": "estimated",
                        },
                    },
                    "CVX": {
                        "2026-10-30": {
                            "timing": "bmo",
                            "source": "nasdaq earnings calendar (time-pre-market)",
                            "fetched_at": "t",
                            "status": "estimated",
                        }
                    },
                }
            )
        )

    def test_upcoming_earnings_merges_sealed_and_estimated(self, tmp_path: Path) -> None:
        paper = _paper(tmp_path)
        self._timing(paper)
        got = events.upcoming_earnings("AAPL", date(2026, 10, 1), date(2026, 10, 31))
        assert [(e.date.isoformat(), e.status, e.timing, e.blocker_only) for e in got] == [
            ("2026-10-28", "estimated", "unknown", True),
            ("2026-10-29", "sealed", "amc", False),
        ]

    def test_no_data_is_an_empty_list_never_a_default(self, tmp_path: Path) -> None:
        _paper(tmp_path)
        assert events.upcoming_earnings("KO", date(2026, 10, 1), date(2026, 12, 31)) == []
        assert events.upcoming_earnings("SPY", date(2026, 10, 1), date(2026, 12, 31)) == []

    def test_missing_sealed_calendar_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(events.EventsError):
            events.upcoming_earnings("AAPL", date(2026, 10, 1), date(2026, 10, 31))

    def test_etf_holding_reports(self, tmp_path: Path) -> None:
        paper = _paper(tmp_path)
        self._timing(paper)
        xle = events.etf_holding_reports("XLE", date(2026, 10, 1), date(2026, 10, 31))
        assert xle.mapped
        assert [(n, e.date.isoformat()) for n, e in xle.reports] == [
            ("XOM", "2026-10-30"),
            ("CVX", "2026-10-30"),
        ]
        assert xle.uncovered == ()
        xlf = events.etf_holding_reports("XLF", date(2026, 10, 1), date(2026, 10, 31))
        assert set(xlf.uncovered) == {"BRK.B", "JPM", "V", "MA"}  # no data in this fixture
        spy = events.etf_holding_reports("SPY", date(2026, 10, 1), date(2026, 10, 31))
        assert not spy.mapped and spy.reports == ()

    def test_holdings_map_covers_the_brief_etfs(self) -> None:
        assert set(events.ETF_HOLDINGS) == {"SMH", "SOXX", "XLE", "XLV", "XLF", "QQQ"}
        assert all(1 <= len(v) <= 7 for v in events.ETF_HOLDINGS.values())


# --------------------------------------------------------------- CLI


class TestEventsCli:
    def test_update_events_cli_line(
        self, tmp_path: Path, trex_calendar, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _paper(tmp_path)
        _seal(tmp_path, trex_calendar)
        rc = run_cli(
            ["update-events", "--horizon", "3"],
            get=Web(_routes()),
            sleep=lambda _s: None,
            now=SATURDAY,
            cal=trex_calendar,
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "update-events nasdaq=3/3 estimated=2 edgar=skipped" in out
        assert "sec_ua_missing" in out and "macro=ok" in out

    def test_seal_macro_cli_from_a_saved_page(
        self, tmp_path: Path, trex_calendar, capsys: pytest.CaptureFixture[str]
    ) -> None:
        html = tmp_path / "fomc.html"
        shutil.copy(FIX / "fomccalendars_trimmed.html", html)
        rc = run_cli(
            [
                "seal-macro",
                "--from",
                "2026-01-01",
                "--to",
                "2027-12-31",
                "--fomc-html",
                str(html),
                "--fetched-on",
                "2026-09-23",
            ],
            now=SATURDAY,
            cal=trex_calendar,
        )
        assert rc == 0
        doc = events.load_macro(tmp_path / "events" / "macro-2026-2027.json")
        assert [f["date"] for f in doc["fomc"]] == FOMC_2026 + FOMC_2027
        assert doc["fomc_source"]["fetched"] == "2026-09-23"
        # --fomc-html without --fetched-on is refused (provenance)
        assert (
            run_cli(
                [
                    "seal-macro",
                    "--from",
                    "2026-01-01",
                    "--to",
                    "2027-12-31",
                    "--fomc-html",
                    str(html),
                ],
                now=SATURDAY,
                cal=trex_calendar,
            )
            == 2
        )
