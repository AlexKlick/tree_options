"""scripts/desk/fetch_ohlc_xu.py — the XU-62 panel extension fetch
(XU-SUCCESSOR-001 §6 item 1), driven entirely on fake vendor bodies.

No network: the vendor client is a routing fake, the paper dir is a tmp dir
(``DESK_PAPER_DIR``), and the roster is asserted against an INDEPENDENT
literal (pinned to the frozen panel sha256 recorded in the prereg §4.1).
"""

from __future__ import annotations

import ast
import json
import re
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from scripts.desk import fetch_ohlc_xu as fx

from tests.fixtures.massive_responses import FakeResponse, FakeTransport, transport_of
from tree_options.data.massive_client import MassiveClient, RateGovernor
from tree_options.time.synthetic import SyntheticCalendar

# The frozen 62-name roster, INDEPENDENTLY written out (derived from
# ohlc-panel-xu.json at sha256 3753dcb8…036c0a5a — XU-SUCCESSOR-001 §4.1).
EXPECTED_ROSTER = (
    "ABBV",
    "ABT",
    "ACN",
    "ADI",
    "AMGN",
    "APO",
    "AXP",
    "BA",
    "BK",
    "BLK",
    "BMY",
    "BRK.B",
    "C",
    "CAT",
    "CME",
    "COF",
    "COP",
    "CVX",
    "DD",
    "DE",
    "DHR",
    "DOW",
    "EMR",
    "F",
    "FCX",
    "GE",
    "GILD",
    "GM",
    "HON",
    "ICE",
    "ISRG",
    "JNJ",
    "KHC",
    "KKR",
    "LIN",
    "LMT",
    "MCD",
    "MDLZ",
    "MMM",
    "MO",
    "MRK",
    "MU",
    "NKE",
    "NUE",
    "PFE",
    "PLD",
    "PNC",
    "RTX",
    "SBUX",
    "SCHW",
    "SPG",
    "SYK",
    "T",
    "TGT",
    "TMO",
    "TXN",
    "UNP",
    "USB",
    "VZ",
    "WBA",
    "WFC",
    "WMT",
)
EXPECTED_PANEL_SHA256 = "3753dcb862eafd67cb9635116b5cb66ed056891dd8251cb4c73ad25a036c0a5a"

D = "2026-11-13"  # the extend target session
PREV = "2026-11-12"

SYMBOL = re.compile(r"^[A-Z.]{1,6}$")


def _ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp() * 1000)


def _bar(day: str, close: str = "100.00") -> dict[str, Any]:
    return {"t": _ms(day), "o": close, "h": close, "l": close, "c": close, "v": 1_000}


def _body(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"status": "OK", "results": list(rows)}


def _capped_body(close: str) -> dict[str, Any]:
    sessions = SyntheticCalendar(date(2024, 12, 16), n_sessions=500).sessions()
    assert sessions[-1].isoformat() == D
    return _body(*(_bar(d.isoformat(), close) for d in sessions))


def _client(transport: FakeTransport) -> MassiveClient:
    return MassiveClient(
        api_key="test-key", transport=transport, cache_dir=None, governor=RateGovernor(None)
    )


class FakeVendor:
    """Routes /v2/aggs range requests; records every (ticker, start, end)."""

    def __init__(self, route) -> None:
        self.route = route
        self.calls: list[tuple[str, str, str]] = []

    def get_json(self, path: str, params=None, *, use_cache: bool = True):
        parts = path.strip("/").split("/")
        assert parts[0] == "v2" and parts[1] == "aggs", path
        ticker, start, end = parts[3], parts[7], parts[8]
        assert params == {"adjusted": "true"} and use_cache is False
        self.calls.append((ticker, start, end))
        out = self.route(ticker, start, end)
        if isinstance(out, Exception):
            raise out
        return out


@pytest.fixture
def paper(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("DESK_PAPER_DIR", str(tmp_path))
    return tmp_path


def _stored_panel(*, drop: str | None = None, close: str = "100.00") -> dict[str, Any]:
    days = ["2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11", PREV]
    panel = {
        n: {
            d: {"close": close, "open": close, "high": close, "low": close, "volume": 1}
            for d in days
        }
        for n in EXPECTED_ROSTER
    }
    # the two delisted names end long before the target (their real shape)
    for n in ("BK", "WBA"):
        panel[n] = {
            "2025-06-02": {"close": close, "volume": 1},
            "2025-06-03": {"close": close, "volume": 1},
        }
    if drop:
        del panel[drop]
    return panel


def _route_ok(ticker: str, start: str, end: str, *, close: str = "101.00") -> dict[str, Any]:
    """A healthy vendor: the overlap joins at the stored close (no phantom
    adjustment move), the target session is present at a new close, and the
    two delisted names end at their historical truncation."""
    if ticker in ("BK", "WBA"):
        return _body(_bar("2025-06-02", "100.00"), _bar("2025-06-03", "100.00"))
    return _body(_bar(start, "100.00"), _bar(end, close))


# ------------------------------------------------------------------ roster


def test_roster_is_pinned_at_62_unique_ticker_names() -> None:
    assert fx.XU_ROSTER == EXPECTED_ROSTER
    assert len(fx.XU_ROSTER) == 62
    assert len(set(fx.XU_ROSTER)) == 62
    assert all(SYMBOL.fullmatch(n) for n in fx.XU_ROSTER)


def test_roster_pin_names_the_frozen_panel_sha() -> None:
    assert fx.XU_PANEL_SHA256_FROZEN == EXPECTED_PANEL_SHA256


def test_roster_is_a_literal_not_a_runtime_universe_read() -> None:
    # the chain-recorder enrollment trap: the roster must never be derived
    # at runtime from desk-universe.toml (or anything else)
    tree = ast.parse(Path(fx.__file__).read_text())
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any("universe" in name for name in imports)
    assert not any("tomllib" in name for name in imports)
    src = Path(fx.__file__).read_text()
    assert "tomllib" not in src  # and no textual TOML read either
    assert fx.SYMBOL_MAP == {}  # no remap found by the 2026-11-16 probe
    assert set(fx.DELISTED) == {"BK", "WBA"}


# ------------------------------------------------------------------- extend


def test_extend_merges_target_and_records_provenance(paper: Path) -> None:
    panel_path = paper / "ohlc-panel-xu.json"
    panel_path.write_text(json.dumps(_stored_panel()))
    vendor = FakeVendor(lambda t, s, e: _route_ok(t, s, e))

    rc = fx.main([D], client=vendor)

    assert rc == 0
    merged = json.loads(panel_path.read_text())
    assert merged["GE"][D]["close"] == "101.00"  # the target bar landed
    assert D not in merged["BK"] and D not in merged["WBA"]  # kept truncated
    prov = (paper / "ohlc-panel-xu.provenance.md").read_text()
    assert "| extend |" in prov
    assert f"truncate BK kept | {fx.DELISTED['BK']}" in prov  # never silent
    assert f"truncate WBA kept | {fx.DELISTED['WBA']}" in prov
    assert not list(paper.glob(".*tmp*"))  # atomic write left no temp files


def test_vendor_lag_exits_3_and_writes_nothing(paper: Path) -> None:
    panel_path = paper / "ohlc-panel-xu.json"
    panel_path.write_text(json.dumps(_stored_panel()))
    before = panel_path.read_bytes()

    def route(ticker: str, start: str, end: str):
        if ticker == "TGT":  # a live name the vendor has not published yet
            return _body(_bar(start))
        return _route_ok(ticker, start, end)

    rc = fx.main([D], client=FakeVendor(route))

    assert rc == 3
    assert panel_path.read_bytes() == before  # all-or-nothing
    assert not (paper / "ohlc-panel-xu.provenance.md").exists()


def test_moved_adjustment_rebases_the_full_window(paper: Path) -> None:
    panel_path = paper / "ohlc-panel-xu.json"
    panel_path.write_text(json.dumps(_stored_panel()))
    # the vendor re-adjusted every close by -10% (a new split): the overlap
    # no longer joins and the full window must be re-fetched and REPLACED
    reval = "90.00"

    def route(ticker: str, start: str, end: str):
        if ticker != "WMT":
            return _route_ok(ticker, start, end)
        if start == fx.BACKFILL_START:  # the rebase full-window request
            days = [f"2025-{m:02d}-{d:02d}" for m in range(1, 11) for d in range(1, 29)]
            return _body(*(_bar(d, reval) for d in days), _bar(D, reval))
        return _body(_bar(start, reval), _bar(end, reval))

    vendor = FakeVendor(route)
    rc = fx.main([D], client=vendor)

    assert rc == 0
    assert ("WMT", fx.BACKFILL_START, D) in vendor.calls  # full re-fetch
    merged = json.loads(panel_path.read_text())
    assert merged["WMT"][D]["close"] == reval
    prov = (paper / "ohlc-panel-xu.provenance.md").read_text()
    assert f"| rebase WMT | {fx.BACKFILL_START}..{D}" in prov


def test_short_full_refetch_is_a_vendor_rejection(paper: Path) -> None:
    panel_path = paper / "ohlc-panel-xu.json"
    panel_path.write_text(json.dumps(_stored_panel()))
    before = panel_path.read_bytes()

    def route(ticker: str, start: str, end: str):
        if ticker != "WMT":
            return _route_ok(ticker, start, end)
        if start == fx.BACKFILL_START:  # a broken short full refetch
            return _body(_bar("2026-01-05", "90.00"), _bar(D, "90.00"))
        return _body(_bar(start, "90.00"), _bar(end, "90.00"))

    assert fx.main([D], client=FakeVendor(route)) == 3
    assert panel_path.read_bytes() == before


def test_entitlement_stop_returns_the_fetched_bars() -> None:
    transport = transport_of(
        json.dumps(_capped_body("90.00")),
        json.dumps({"status": "NOT_AUTHORIZED", "message": "Historical access denied"}),
    )
    bars = fx.fetch_range(_client(transport), "ABBV", fx.BACKFILL_START, D)

    assert [url.rsplit("/", 1)[1] for url in transport.paths()] == [D, "2024-12-15"]
    assert len(bars) == 500
    assert bars["2024-12-16"]["close"] == bars[D]["close"] == "90.00"
    assert bars.refusal and "Historical access denied" in bars.refusal


def test_entitlement_short_rebase_exits_3_without_writing(paper: Path, capsys) -> None:
    panel = _stored_panel()
    panel["WMT"][fx.BACKFILL_START] = panel["WMT"][PREV]
    panel_path = fx.panel_path(paper)
    panel_path.write_text(json.dumps(panel))
    before = panel_path.read_bytes()
    provenance = fx.provenance_path(paper)
    provenance.write_bytes(b"prior extend\n")

    # WMT is last. Its overlap moves, then the full refetch serves the target
    # and 499 other sessions before refusing the older, previously stored head.
    message = "Your plan doesn't include this data timeframe."
    transport = transport_of(
        *(json.dumps(_route_ok(n, PREV, D)) for n in EXPECTED_ROSTER[:-1]),
        json.dumps(_body(_bar(PREV, "90.00"), _bar(D, "90.00"))),
        json.dumps(_capped_body("90.00")),
        json.dumps({"status": "NOT_AUTHORIZED", "message": message}),
    )

    assert fx.main([D], client=_client(transport)) == 3
    assert [url.split("/ticker/")[1] for url in transport.paths()[-3:]] == [
        f"WMT/range/1/day/2026-11-06/{D}",
        f"WMT/range/1/day/{fx.BACKFILL_START}/{D}",
        f"WMT/range/1/day/{fx.BACKFILL_START}/2024-12-15",
    ]
    summary = capsys.readouterr().out.splitlines()[-1]
    assert summary == (
        f"vendor rejected: WMT: incomplete range {fx.BACKFILL_START}..{D} (500 bars fetched): "
        f"/v2/aggs/ticker/WMT/range/1/day/{fx.BACKFILL_START}/2024-12-15?adjusted=true: "
        f"not entitled on this API tier — vendor says: {message}"
    )
    assert panel_path.read_bytes() == before
    assert provenance.read_bytes() == b"prior extend\n"


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (
            200,
            {"status": "NOT_AUTHORIZED", "message": "History unavailable"},
            "History unavailable",
        ),
        (403, {"message": "data timeframe"}, "data timeframe"),
        (401, {"message": "invalid key"}, "key rejected"),
    ],
)
def test_first_request_refusal_is_reported(paper: Path, capsys, status, body, reason) -> None:
    transport = FakeTransport([FakeResponse(status=status, body=json.dumps(body))])

    assert fx.main([D], client=_client(transport)) == 3
    assert transport.calls == 1
    summary = capsys.readouterr().out.splitlines()[-1]
    assert summary.startswith("vendor rejected: ABBV: ")
    assert reason in summary
    if status != 401:
        assert "(0 bars fetched)" in summary
    assert not fx.panel_path(paper).exists()
    assert not fx.provenance_path(paper).exists()


def test_500_cap_head_chase_merges_the_head_range(paper: Path) -> None:
    panel_path = paper / "ohlc-panel-xu.json"
    head_boundary = "2024-12-15"

    def route(ticker: str, start: str, end: str):
        if ticker != "ABBV":
            return _route_ok(ticker, start, end)
        if end == head_boundary:  # the residual head range
            return _body(_bar("2024-06-16", "98.00"), _bar("2024-12-13", "98.00"))
        return _capped_body("100.00")

    # ABBV's stored series is stale (ends 2024-06): its overlap range spans
    # more than 500 sessions, so the vendor caps the head
    panel = _stored_panel()
    panel["ABBV"] = {f"2024-06-{d:02d}": {"close": "98.00", "volume": 1} for d in range(1, 21)}
    panel_path.write_text(json.dumps(panel))

    vendor = FakeVendor(route)
    rc = fx.main([D], client=vendor)

    assert rc == 0
    abbv_calls = [c for c in vendor.calls if c[0] == "ABBV"]
    assert len(abbv_calls) == 2  # the capped request + the head chase
    assert abbv_calls[1] == ("ABBV", "2024-06-16", head_boundary)
    merged = json.loads(panel_path.read_text())
    assert merged["ABBV"]["2024-12-13"]["close"] == "98.00"
    assert merged["ABBV"][D]["close"] == "100.00"


def test_roster_drift_refuses_to_write(paper: Path) -> None:
    panel_path = paper / "ohlc-panel-xu.json"
    panel_path.write_text(json.dumps(_stored_panel(drop="MU")))
    before = panel_path.read_bytes()

    rc = fx.main([D], client=FakeVendor(lambda t, s, e: _route_ok(t, s, e)))

    assert rc == 1
    assert panel_path.read_bytes() == before
    assert not (paper / "ohlc-panel-xu.provenance.md").exists()


def test_merge_happens_under_the_exclusive_panel_lock(paper: Path) -> None:
    panel_path = paper / "ohlc-panel-xu.json"
    panel_path.write_text(json.dumps(_stored_panel()))
    lock = paper / "ohlc-panel-xu.json.lock"
    lock.touch()

    reached_last_name = threading.Event()

    def route(ticker: str, start: str, end: str):
        if ticker == "WMT":  # the roster's last name: fetch loop is done next
            reached_last_name.set()
        return _route_ok(ticker, start, end)

    result: dict[str, Any] = {}

    def run() -> None:
        result["rc"] = fx.main([D], client=FakeVendor(route))

    with lock.open("a") as fh:  # the test holds LOCK_EX like a rival writer
        import fcntl

        fcntl.flock(fh, fcntl.LOCK_EX)
        thread = threading.Thread(target=run)
        thread.start()
        assert reached_last_name.wait(5), "fetch loop never reached the last name"
        thread.join(0.4)
        assert thread.is_alive(), "extend wrote while a rival held the lock"
        fcntl.flock(fh, fcntl.LOCK_UN)
    thread.join(10)
    assert result["rc"] == 0
    assert D in json.loads(panel_path.read_text())["GE"]
