"""Desk D6 point-in-time loaders: each source is cut at what a decision for
session D could know at its cutoff (the next session's 09:30 ET open).

Oracles are literal dates and values written here; the HAR wiring is
checked against the validated ``har.forecasts_at`` on the UNCUT inputs.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.fixtures import desk_pricing as fx
from tree_options.desk import har, pit
from tree_options.time.calendar import StaticSessionCalendar

ET = ZoneInfo("America/New_York")
D = date(2025, 3, 12)  # a Wednesday; cutoff 2025-03-13 09:30 EDT
NAMES = ("AAA", "SPY", "QQQ")

SEALED = {
    "AAA": [
        "2023-04-27",
        "2023-07-27",
        "2023-10-26",
        "2024-01-25",
        "2024-04-25",
        "2024-07-25",
        "2024-10-24",
        "2025-01-23",
        "2025-04-24",  # future at D: no vintage, not knowable
        "2025-07-24",
    ],
    "SPY": [],
}


def _t(timing: str, status: str, fetched_at: str, source: str = "test") -> dict[str, str]:
    return {"timing": timing, "status": status, "fetched_at": fetched_at, "source": source}


TIMING = {
    "AAA": {
        "2023-01-26": _t("amc", "confirmed", "2026-09-01T12:00:00-04:00"),  # a past record
        "2025-01-23": _t("amc", "confirmed", "2026-09-01T12:00:00-04:00"),
        "2024-12-15": _t("unknown", "estimated", "2024-12-01T21:00:00-05:00"),
        "2025-03-19": _t("unknown", "estimated", "2025-03-13T09:29:00-04:00"),  # 1 min early
        "2025-03-20": _t("unknown", "estimated", "2025-03-13T09:31:00-04:00"),  # 1 min late
        "2025-04-10": _t("bmo", "estimated", "2025-03-03T21:00:00-05:00"),
        "2025-07-24": _t("unknown", "estimated", "2025-06-01T21:00:00-04:00"),
    }
}


@pytest.fixture(scope="module")
def cal() -> StaticSessionCalendar:
    return fx.trex_calendar()


@pytest.fixture(scope="module")
def panel(cal: StaticSessionCalendar) -> dict[str, dict[str, Any]]:
    return fx.synthetic_panel(cal, NAMES, date(2023, 1, 3), date(2025, 6, 30), seed=1)


def _sources(store: Path, panel: dict[str, Any], **over: Any) -> pit.Sources:
    kw: dict[str, Any] = {
        "panel": panel,
        "sealed": SEALED,
        "timing": TIMING,
        "iv_history": None,
        "store": store,
    }
    kw.update(over)
    return pit.Sources(**kw)


# ---------------------------------------------------------------- cutoff


@pytest.mark.parametrize(
    ("session", "expected"),
    [
        (date(2025, 3, 12), datetime(2025, 3, 13, 9, 30, tzinfo=ET)),
        (date(2025, 3, 14), datetime(2025, 3, 17, 9, 30, tzinfo=ET)),  # Friday -> Monday
        (date(2025, 4, 17), datetime(2025, 4, 21, 9, 30, tzinfo=ET)),  # Good Friday closed
        (date(2025, 3, 7), datetime(2025, 3, 10, 9, 30, tzinfo=ET)),  # across the DST switch
    ],
)
def test_decision_cutoff_is_the_next_session_open(
    cal: StaticSessionCalendar, session: date, expected: datetime
) -> None:
    got = pit.decision_cutoff(session, cal)
    assert got == expected
    assert got.utcoffset() == expected.utcoffset()


def test_non_session_is_refused(cal: StaticSessionCalendar, tmp_path: Path) -> None:
    with pytest.raises(pit.NotEvaluable):
        pit.PointInTime(date(2025, 3, 15), cal, _sources(tmp_path, {}))


# ------------------------------------------------------------------ bars


def test_bars_are_cut_at_the_session(
    cal: StaticSessionCalendar, panel: dict[str, Any], tmp_path: Path
) -> None:
    p = pit.PointInTime(D, cal, _sources(tmp_path, panel))
    bars = p.bars("AAA")
    assert max(bars) == "2025-03-12"
    assert bars == {d: b for d, b in panel["AAA"].items() if d <= "2025-03-12"}
    assert p.bars("NOPE") == {}


# -------------------------------------------------------------- earnings


def test_earnings_keep_record_estimate_and_upcoming_apart(
    cal: StaticSessionCalendar, panel: dict[str, Any], tmp_path: Path
) -> None:
    e = pit.PointInTime(D, cal, _sources(tmp_path, panel)).earnings("AAA")
    past = [(x.date.isoformat(), x.status, x.timing) for x in e.past]
    assert past == [
        ("2023-01-26", "confirmed", "amc"),  # EDGAR record, not in the sealed file
        ("2023-04-27", "sealed", "unknown"),
        ("2023-07-27", "sealed", "unknown"),
        ("2023-10-26", "sealed", "unknown"),
        ("2024-01-25", "sealed", "unknown"),
        ("2024-04-25", "sealed", "unknown"),
        ("2024-07-25", "sealed", "unknown"),
        ("2024-10-24", "sealed", "unknown"),
        ("2025-01-23", "sealed", "amc"),  # sealed, timed by the confirmation
    ]
    assert [(x.date.isoformat(), x.status) for x in e.past_estimated] == [
        ("2024-12-15", "estimated")
    ]
    # dated after D: only what was fetched by the cutoff; never the sealed dates
    upcoming = [(x.date.isoformat(), x.status, x.timing, x.blocker_only) for x in e.upcoming]
    assert upcoming == [
        ("2025-03-19", "estimated", "unknown", True),
        ("2025-04-10", "estimated", "bmo", True),
    ]
    assert e.reporter
    assert e.known_dates() == sorted(
        {x.date.isoformat() for x in (*e.past, *e.past_estimated, *e.upcoming)}
    )


def test_schedule_assumption_marks_sealed_future_dates(
    cal: StaticSessionCalendar, panel: dict[str, Any], tmp_path: Path
) -> None:
    p = pit.PointInTime(D, cal, _sources(tmp_path, panel), schedule_assumption=True)
    upcoming = [(x.date.isoformat(), x.status) for x in p.earnings("AAA").upcoming]
    assert upcoming == [
        ("2025-03-19", "estimated"),
        ("2025-04-10", "estimated"),
        ("2025-04-24", "sealed-assumed"),
        ("2025-07-24", "sealed-assumed"),  # the sealed date; the late estimate stays unknown
    ]


def test_etf_has_no_earnings(
    cal: StaticSessionCalendar, panel: dict[str, Any], tmp_path: Path
) -> None:
    e = pit.PointInTime(D, cal, _sources(tmp_path, panel)).earnings("SPY")
    assert not e.reporter
    assert (e.past, e.past_estimated, e.upcoming) == ((), (), ())


# ------------------------------------------------------------ IV history


def test_iv_history_is_cut_at_the_session(
    cal: StaticSessionCalendar, panel: dict[str, Any], tmp_path: Path
) -> None:
    doc = fx.iv_history_doc(
        {"AAA": {"2025-03-11": 0.21, "2025-03-12": 0.22, "2025-03-13": 0.99, "2025-03-10": None}}
    )
    p = pit.PointInTime(D, cal, _sources(tmp_path, panel, iv_history=doc))
    assert p.iv_history("AAA") == {
        date(2025, 3, 11): (0.21, "interpolated"),
        date(2025, 3, 12): (0.22, "interpolated"),
    }
    assert p.iv_history("BBB") == {}
    assert pit.PointInTime(D, cal, _sources(tmp_path, panel)).iv_history("AAA") == {}


# ---------------------------------------------------------------- chains


def _doc(session: date, spot: float, as_of: str) -> dict[str, Any]:
    return fx.chain_doc(
        sym="AAA",
        session=session,
        spot=spot,
        rate=0.04,
        expiries=[date(2025, 5, 16)],
        strikes=[95.0, 100.0, 105.0],
        iv=fx.smile,
        half_spread=fx.half_spread,
        source_as_of=as_of,
    )


def test_chain_reads_only_the_canonical_session_file(
    cal: StaticSessionCalendar, panel: dict[str, Any], tmp_path: Path
) -> None:
    fx.write_chain(tmp_path, _doc(D, 100.0, "2025-03-13T03:49:00+00:00"))
    fx.write_chain(tmp_path, _doc(D, 111.0, "2025-03-13T15:00:00+00:00"), conflict=True)
    fx.write_chain(tmp_path, _doc(date(2025, 3, 13), 122.0, "2025-03-14T03:49:00+00:00"))
    doc = pit.PointInTime(D, cal, _sources(tmp_path, panel)).chain("AAA")
    assert doc["header"]["underlying_quote"]["close"] == 100.0


def test_chain_after_the_cutoff_or_mislabeled_is_refused(
    cal: StaticSessionCalendar, panel: dict[str, Any], tmp_path: Path
) -> None:
    p = pit.PointInTime(D, cal, _sources(tmp_path, panel))
    with pytest.raises(pit.NotEvaluable, match="no recorded chain"):
        p.chain("AAA")
    # the vendor snapshot is stamped 10:00 EDT, after the 09:30 cutoff
    fx.write_chain(tmp_path, _doc(D, 100.0, "2025-03-13T14:00:00+00:00"))
    with pytest.raises(pit.NotEvaluable, match="cutoff"):
        p.chain("AAA")
    # a file in D's directory whose header describes another session
    wrong = _doc(date(2025, 3, 11), 100.0, "2025-03-12T03:49:00+00:00")
    path = fx.write_chain(tmp_path, wrong)
    target = tmp_path / "chains" / D.isoformat() / "AAA.json.gz"
    target.write_bytes(path.read_bytes())
    with pytest.raises(pit.NotEvaluable, match="session"):
        p.chain("AAA")


# --------------------------------------------------------------- indices


def test_indices_cut_at_d_and_dtb3_one_session_earlier(
    cal: StaticSessionCalendar, panel: dict[str, Any], tmp_path: Path
) -> None:
    fx.write_index(
        tmp_path, "VIX", [("2025-03-11", "20.1"), ("2025-03-12", "21.5"), ("2025-03-13", "40.0")]
    )
    fx.write_index(
        tmp_path,
        "DTB3",
        [
            ("2025-03-07", "4.29"),
            ("2025-03-10", "4.30"),
            ("2025-03-11", "4.31"),
            ("2025-03-12", "4.32"),
        ],
    )
    p = pit.PointInTime(D, cal, _sources(tmp_path, panel))
    assert p.index("VIX") == {date(2025, 3, 11): "20.1", date(2025, 3, 12): "21.5"}
    # FRED posts D's DTB3 the next afternoon: the last knowable row is D-1's
    assert max(p.index("DTB3")) == date(2025, 3, 11)
    assert p.rate() == pytest.approx(0.0431, abs=1e-15)
    assert p.index("NOPE") == {}


def test_rate_skips_fred_empty_rows_and_is_none_without_data(
    cal: StaticSessionCalendar, panel: dict[str, Any], tmp_path: Path
) -> None:
    assert pit.PointInTime(D, cal, _sources(tmp_path, panel)).rate() is None
    fx.write_index(tmp_path, "DTB3", [("2025-03-10", "4.30"), ("2025-03-11", "")])
    assert pit.PointInTime(D, cal, _sources(tmp_path, panel)).rate() == pytest.approx(0.043)


# ------------------------------------------------------------------- HAR


def test_har_equals_the_validated_forecast_on_uncut_inputs(
    cal: StaticSessionCalendar, panel: dict[str, Any], tmp_path: Path
) -> None:
    """The PIT HAR is FORECAST-001's model: fed the UNCUT panel and sealed
    calendar plus the same forward schedule, har.forecasts_at gives the
    same numbers (the fit never reads anything after D)."""
    p = pit.PointInTime(D, cal, _sources(tmp_path, panel), har_names=NAMES)
    got = p.har(20, min_event_rows=10)
    known = p.earnings("AAA").known_dates()
    fwd = {"AAA": known, "SPY": [], "QQQ": []}
    ref = har.forecasts_at(
        panel, SEALED, cal, NAMES, D, 20, min_event_rows=10, forward_schedule=fwd
    )
    assert set(got) == set(ref) == set(NAMES)
    for name in NAMES:
        assert got[name].forecast == ref[name].forecast
        assert got[name].n_earn == ref[name].n_earn
        assert got[name].schedule == ref[name].schedule
        assert got[name].fit_through == ref[name].fit_through
    # (D, D+20 sessions] = (2025-03-12, 2025-04-09]: the 03-19 estimate is inside,
    # the 04-10 one is past the window, the late 03-20 estimate is unknown
    assert got["AAA"].n_earn == 1.0
    assert got["AAA"].be is not None
    # the no-event baseline: the same model with no report ahead
    past_only = {"AAA": [d for d in known if d <= D.isoformat()], "SPY": [], "QQQ": []}
    ex = har.forecasts_at(
        panel, SEALED, cal, NAMES, D, 20, min_event_rows=10, forward_schedule=past_only
    )
    assert ex["AAA"].n_earn == 0.0
    assert got["AAA"].forecast_ex_events == ex["AAA"].forecast
    assert got["AAA"].forecast_ex_events < got["AAA"].forecast
    assert got["SPY"].forecast_ex_events == got["SPY"].forecast


def test_har_is_empty_when_the_panel_lacks_the_session(
    cal: StaticSessionCalendar, panel: dict[str, Any], tmp_path: Path
) -> None:
    short = {n: {d: b for d, b in bars.items() if d < "2025-03-12"} for n, bars in panel.items()}
    p = pit.PointInTime(D, cal, _sources(tmp_path, short), har_names=NAMES)
    assert p.har(20) == {}
