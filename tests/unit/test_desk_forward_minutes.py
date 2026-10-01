"""Forward Massive minute-bar corpus at the decision clocks (A1 lane).

``desk/cost.py``'s provenance block names the gap this module closes: the
CBOE delayed feed is an EOD snapshot (byte-stable during RTH), so nothing
on disk can describe a 10:00/10:15/15:15 ET fill. The one intraday source
the repo already trusts is Massive per-contract minute aggregates (the
1.12M-bar longrun bundle was built from them). These tests pin the three
moving parts of the forward corpus:

* the contract-selection refresh (measured-universe filters, monthly
  expiries, the wire-bounding cap, provenance, write-once);
* the wire budget guard (headroom rule, stop-on-exceed, the ledger);
* the timer schedule as shipped in ``deploy/desk/`` (parity with the
  module's constants, and the capture-window invariant that every session
  is reachable by a slot after its options close).

No network: the MassiveClient gets an injected transport. Every store /
forward / cache path is pinned to tmp. The bars a fake wire serves are
synthetic full-session minutes.
"""

from __future__ import annotations

import gzip
import hashlib
import argparse
import json
import re
import urllib.parse
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tree_options.data.massive_client import (
    BackoffPolicy,
    HttpResponse,
    MassiveClient,
    RateGovernor,
)
from tree_options.desk import forward_minutes as fm
from tree_options.desk.__main__ import run_cli

ET = ZoneInfo("America/New_York")
DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "desk"
KEY = "FAKEKEY-forward-minutes-0123456789"

SELECT_ON = date(2026, 10, 3)  # Saturday: the weekly selection slot
CHAIN_D = date(2026, 10, 2)  # Friday: the chain session the selection reads
SESSION = date(2026, 10, 5)  # Monday: the captured session
NOW = datetime(2026, 10, 5, 17, 30, tzinfo=ET)  # after the 16:15 options close

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_ONCAL = re.compile(
    r"^OnCalendar=(?P<days>(?:\*|Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:\.\.(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun))?)"
    r" \*-\*-\* (?P<hh>\d\d):(?P<mm>\d\d):(?P<ss>\d\d) (?P<tz>[A-Za-z_]+/[A-Za-z_]+)$"
)

# three Friday expiries around the selection date:
MONTHLY_NEAR = date(2026, 10, 16)  # third Friday, dte(SELECT_ON) = 13
MONTHLY_FAR = date(2026, 11, 20)  # third Friday, dte = 48
WEEKLY = date(2026, 10, 23)  # a Friday that is NOT the third (day 23 > 21)
TOO_FAR = date(2026, 12, 18)  # monthly but dte = 76 > 60
NOT_FRIDAY = date(2026, 10, 8)  # Thursday, dte = 5 < 7


def _occ(root: str, expiry: date, right: str, strike: float) -> str:
    return f"{root}{expiry:%y%m%d}{right}{round(strike * 1000):08d}"


def _chain_doc(sym: str, session: date, rows: list[dict], spot: float) -> bytes:
    """A desk chain document (store.build_document shape) for ``sym``."""
    rows = sorted(rows, key=lambda r: (r["expiry"], r["right"], r["strike"], r["occ"]))
    columns: dict[str, list] = {c: [] for c in (
        "occ", "exp", "right", "strike", "bid", "ask", "iv", "delta", "gamma",
        "theta", "vega", "rho", "theo", "oi", "volume", "last",
        "last_time")}
    for r in rows:
        columns["occ"].append(r["occ"])
        columns["exp"].append(r["expiry"].isoformat())
        columns["right"].append(r["right"])
        columns["strike"].append(float(r["strike"]))
        for col in ("bid", "ask", "iv", "gamma", "theta", "vega", "rho", "theo", "last"):
            columns[col].append(None)
        columns["delta"].append(r.get("delta"))
        columns["oi"].append(r.get("open_interest", 100))
        columns["volume"].append(r.get("volume", 50))
        columns["last_time"].append(None)
    doc = {
        "header": {
            "schema": "desk-chain/1",
            "session": session.isoformat(),
            "underlying": sym,
            "source": "cboe delayed",
            "source_as_of": f"{session.isoformat()}T23:49:00+00:00",
            "fetched_at": f"{session.isoformat()}T23:50:00+00:00",
            "underlying_quote": {"current_price": spot, "bid": spot - 0.02, "ask": spot + 0.02},
            "raw_sha256": hashlib.sha256(f"{sym}{session}".encode()).hexdigest(),
            "n": len(rows),
            "n_skipped": 0,
        },
        "columns": columns,
    }
    return gzip.compress(
        (json.dumps(doc, separators=(",", ":"), sort_keys=True) + "\n").encode(), mtime=0
    )


def _write_chain(store_root: Path, session: date, sym: str, rows: list[dict], spot: float) -> None:
    target = store_root / "chains" / session.isoformat() / f"{sym}.json.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_chain_doc(sym, session, rows, spot))


# The IWM panel: spot 240. The nearest-strike pick must dodge the |delta|
# and liquidity exclusions (242C has volume 0, 238P has oi 0, 236C is
# |delta| 0.75 > 0.70), so the picks are NOT simply the 3 nearest strikes.
IWM_ROWS = [
    # (10-16 calls) 236 excluded on delta, 242 excluded on volume
    {"occ": _occ("IWM", MONTHLY_NEAR, "C", 236), "expiry": MONTHLY_NEAR, "right": "C",
     "strike": 236, "delta": 0.75},
    {"occ": _occ("IWM", MONTHLY_NEAR, "C", 238), "expiry": MONTHLY_NEAR, "right": "C",
     "strike": 238, "delta": 0.62},
    {"occ": _occ("IWM", MONTHLY_NEAR, "C", 240), "expiry": MONTHLY_NEAR, "right": "C",
     "strike": 240, "delta": 0.51},
    {"occ": _occ("IWM", MONTHLY_NEAR, "C", 242), "expiry": MONTHLY_NEAR, "right": "C",
     "strike": 242, "delta": 0.38, "volume": 0},
    {"occ": _occ("IWM", MONTHLY_NEAR, "C", 244), "expiry": MONTHLY_NEAR, "right": "C",
     "strike": 244, "delta": 0.25},
    # (10-16 puts) 238 excluded on open_interest
    {"occ": _occ("IWM", MONTHLY_NEAR, "P", 238), "expiry": MONTHLY_NEAR, "right": "P",
     "strike": 238, "delta": -0.62, "open_interest": 0},
    {"occ": _occ("IWM", MONTHLY_NEAR, "P", 240), "expiry": MONTHLY_NEAR, "right": "P",
     "strike": 240, "delta": -0.49},
    {"occ": _occ("IWM", MONTHLY_NEAR, "P", 242), "expiry": MONTHLY_NEAR, "right": "P",
     "strike": 242, "delta": -0.36},
    {"occ": _occ("IWM", MONTHLY_NEAR, "P", 244), "expiry": MONTHLY_NEAR, "right": "P",
     "strike": 244, "delta": -0.28},
    # (11-20 calls) only two pass
    {"occ": _occ("IWM", MONTHLY_FAR, "C", 230), "expiry": MONTHLY_FAR, "right": "C",
     "strike": 230, "delta": 0.68},
    {"occ": _occ("IWM", MONTHLY_FAR, "C", 240), "expiry": MONTHLY_FAR, "right": "C",
     "strike": 240, "delta": 0.55},
    # excluded panels: non-monthly Friday, Thursday, dte 76
    {"occ": _occ("IWM", WEEKLY, "C", 240), "expiry": WEEKLY, "right": "C",
     "strike": 240, "delta": 0.50},
    {"occ": _occ("IWM", NOT_FRIDAY, "C", 240), "expiry": NOT_FRIDAY, "right": "C",
     "strike": 240, "delta": 0.50},
    {"occ": _occ("IWM", TOO_FAR, "C", 240), "expiry": TOO_FAR, "right": "C",
     "strike": 240, "delta": 0.50},
]
IWM_PICKS = {
    "O:" + _occ("IWM", MONTHLY_NEAR, "C", 238),
    "O:" + _occ("IWM", MONTHLY_NEAR, "C", 240),
    "O:" + _occ("IWM", MONTHLY_NEAR, "C", 244),
    "O:" + _occ("IWM", MONTHLY_NEAR, "P", 240),
    "O:" + _occ("IWM", MONTHLY_NEAR, "P", 242),
    "O:" + _occ("IWM", MONTHLY_NEAR, "P", 244),
    "O:" + _occ("IWM", MONTHLY_FAR, "C", 230),
    "O:" + _occ("IWM", MONTHLY_FAR, "C", 240),
}

QQQ_ROWS = [
    {"occ": _occ("QQQ", MONTHLY_NEAR, "C", s), "expiry": MONTHLY_NEAR, "right": "C",
     "strike": s, "delta": d}
    for s, d in ((478, 0.61), (480, 0.50), (482, 0.39))
]
SPY_ROWS = [
    {"occ": _occ("SPY", MONTHLY_NEAR, "C", s), "expiry": MONTHLY_NEAR, "right": "C",
     "strike": s, "delta": d}
    for s, d in ((568, 0.62), (570, 0.51), (572, 0.40))
]


@pytest.fixture()
def pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    root = {
        "store": tmp_path / "desk-store",
        "forward": tmp_path / "desk-forward-minutes",
        "cache": tmp_path / "massive-cache-desk",
        "state": tmp_path / "state",
    }
    monkeypatch.setenv("DESK_STORE", str(root["store"]))
    monkeypatch.setenv("DESK_FORWARD_DIR", str(root["forward"]))
    monkeypatch.setenv("TREX_DESK_STATE", str(root["state"]))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DESK_REPO_ROOT", raising=False)
    return root


@pytest.fixture()
def chain_store(pinned: dict[str, Path]) -> dict[str, Path]:
    _write_chain(pinned["store"], CHAIN_D, "IWM", IWM_ROWS, 240.0)
    _write_chain(pinned["store"], CHAIN_D, "QQQ", QQQ_ROWS, 480.0)
    _write_chain(pinned["store"], CHAIN_D, "SPY", SPY_ROWS, 570.0)
    # an older partial session (only IWM) must not be picked, and a session
    # AFTER the selection date must be ignored even if complete
    _write_chain(pinned["store"], date(2026, 9, 30), "IWM", IWM_ROWS[:2], 240.0)
    _write_chain(pinned["store"], SESSION, "IWM", IWM_ROWS, 240.0)
    _write_chain(pinned["store"], SESSION, "QQQ", QQQ_ROWS, 480.0)
    _write_chain(pinned["store"], SESSION, "SPY", SPY_ROWS, 570.0)
    return pinned


# ------------------------------------------------------------ selection


class TestSelectionRefresh:
    def test_filters_monthlies_dte_delta_and_liquidity(self, chain_store) -> None:
        report = fm.refresh_selection(selected_on=SELECT_ON)
        picked = {c["ticker"] for c in report["contracts"]}
        assert picked == IWM_PICKS | {
            "O:" + t for t in (
                _occ("QQQ", MONTHLY_NEAR, "C", 478),
                _occ("QQQ", MONTHLY_NEAR, "C", 480),
                _occ("QQQ", MONTHLY_NEAR, "C", 482),
                _occ("SPY", MONTHLY_NEAR, "C", 568),
                _occ("SPY", MONTHLY_NEAR, "C", 570),
                _occ("SPY", MONTHLY_NEAR, "C", 572),
            )
        }
        # every pick carries the measured-universe provenance it was made under
        for c in report["contracts"]:
            assert 7 <= c["dte_at_selection"] <= 60
            assert abs(c["delta_at_selection"]) <= 0.70
            assert c["expiry"] in (MONTHLY_NEAR.isoformat(), MONTHLY_FAR.isoformat())

    def test_nearest_strikes_dodge_the_excluded_rows(self, chain_store) -> None:
        report = fm.refresh_selection(selected_on=SELECT_ON)
        iwm = [c for c in report["contracts"] if c["underlying"] == "IWM"]
        near_calls = {c["strike"] for c in iwm
                      if c["expiry"] == MONTHLY_NEAR.isoformat() and c["right"] == "C"}
        # 242C is volume 0 and 236C is |delta| 0.75: the third pick is 244
        assert near_calls == {238.0, 240.0, 244.0}
        near_puts = {c["strike"] for c in iwm
                     if c["expiry"] == MONTHLY_NEAR.isoformat() and c["right"] == "P"}
        assert near_puts == {240.0, 242.0, 244.0}  # 238P is oi 0

    def test_uses_the_latest_chain_session_at_or_before_the_selection_date(
        self, chain_store
    ) -> None:
        report = fm.refresh_selection(selected_on=SELECT_ON)
        assert report["chain_session"] == CHAIN_D.isoformat()

    def test_provenance_hash_and_write_once(self, chain_store) -> None:
        first = fm.refresh_selection(selected_on=SELECT_ON)
        raw = json.dumps(first["source_files"], sort_keys=True).encode()
        assert first["source_sha256"] == hashlib.sha256(raw).hexdigest()
        assert first["execution_authorized"] is False
        again = fm.refresh_selection(selected_on=SELECT_ON)
        assert again["status"] == "exists"
        assert again["source_sha256"] == first["source_sha256"]

    def test_the_cap_skips_whole_groups_and_says_so(self, chain_store) -> None:
        report = fm.refresh_selection(selected_on=SELECT_ON, max_contracts=9)
        # groups fill in expiry order: the three 10-16 groups (9 contracts)
        # fit; the SPY call group and the 11-20 IWM group are skipped whole
        assert len(report["contracts"]) == 9
        skipped = {s["key"] for s in report["skipped_groups"]}
        assert f"IWM/{MONTHLY_FAR.isoformat()}/C" in skipped
        assert all(s["reason"] == "cap" for s in report["skipped_groups"])
        assert report["contracts"][0]["expiry"] == MONTHLY_NEAR.isoformat()

    def test_no_chain_session_for_any_underlying_fails_loudly(self, pinned) -> None:
        _write_chain(pinned["store"], CHAIN_D, "IWM", IWM_ROWS, 240.0)
        with pytest.raises(fm.SelectionError, match="QQQ"):
            fm.refresh_selection(selected_on=SELECT_ON)


# ------------------------------------------------------------ budget


class TestWireBudget:
    def test_headroom_rule_and_stop_on_exceed(self, tmp_path: Path) -> None:
        budget = fm.WireBudget.load(tmp_path / "2026-10-05.json", cap=5)
        assert budget.spent == 0
        assert budget.allow(4) is True
        budget.record("O:IWM260516C00240000", 4)
        assert budget.spent == 4
        assert budget.allow(2) is False  # 4 + 2 > 5: stop
        budget.refuse("O:SPY261016C00570000", 2)
        doc = json.loads((tmp_path / "2026-10-05.json").read_text())
        assert doc["schema"] == "desk-forward-wire-budget/1"
        assert doc["spent"] == 4
        assert doc["cap"] == 5
        assert doc["entries"][0]["label"] == "O:IWM260516C00240000"
        assert doc["entries"][0]["requests"] == 4
        assert doc["refusals"][0]["label"] == "O:SPY261016C00570000"
        assert doc["refusals"][0]["wanted"] == 2

    def test_the_ledger_survives_the_process(self, tmp_path: Path) -> None:
        fm.WireBudget.load(tmp_path / "2026-10-05.json", cap=5).record("a", 3)
        reloaded = fm.WireBudget.load(tmp_path / "2026-10-05.json", cap=5)
        assert reloaded.spent == 3
        assert reloaded.allow(3) is False  # 3 + 3 > 5

    def test_a_spent_ledger_never_goes_backwards_on_refusal(self, tmp_path: Path) -> None:
        budget = fm.WireBudget.load(tmp_path / "d.json", cap=2)
        budget.record("a", 2)
        budget.refuse("b", 1)
        assert budget.spent == 2


# ------------------------------------------------------------ capture


def _session_bars(session: date, end: tuple[int, int] = (16, 14)) -> list[dict]:
    """Synthetic minute bars 09:30..end ET (a late-close session prints to 16:14)."""
    out = []
    moment = datetime.combine(session, time(9, 30), tzinfo=ET)
    last = datetime.combine(session, time(*end), tzinfo=ET)
    while moment <= last:
        out.append({"t": int(moment.timestamp() * 1000), "o": 1.0, "h": 1.02,
                    "l": 0.98, "c": 1.0, "v": 12})
        moment += timedelta(minutes=1)
    return out


class FakeWire:
    def __init__(self, bodies: dict[str, object]) -> None:
        self.bodies = bodies
        self.urls: list[str] = []

    def __call__(self, url: str, *, timeout: float) -> HttpResponse:
        self.urls.append(url)
        # the ticker is a PATH segment (/v2/aggs/ticker/<T>/range/...), not a
        # query param
        m = re.search(r"/v2/aggs/ticker/([^/?]+)/range/", urllib.parse.urlsplit(url).path)
        assert m is not None, url
        ticker = urllib.parse.unquote(m.group(1))
        body = self.bodies.get(ticker, {"status": "OK", "request_id": "r0", "results": []})
        if isinstance(body, Exception):
            raise body
        return HttpResponse(200, json.dumps(
            dict(body, ticker=ticker, resultsCount=len(body["results"]))).encode())


def _client(wire: FakeWire, cache: Path) -> MassiveClient:
    return MassiveClient(
        api_key=KEY,
        transport=wire,
        cache_dir=cache,
        governor=RateGovernor(None),
        backoff=BackoffPolicy(max_attempts=1),
    )


def _full_bodies() -> dict[str, object]:
    return {
        t: {"status": "OK", "request_id": f"r-{i}", "results": _session_bars(SESSION)}
        for i, t in enumerate(sorted(IWM_PICKS))
    }


class TestCapture:
    def test_captures_every_contract_once_and_records_dte_at_session(
        self, chain_store
    ) -> None:
        selection = fm.refresh_selection(selected_on=SELECT_ON)
        wire = FakeWire(_full_bodies() | {
            t: {"status": "OK", "request_id": "q", "results": _session_bars(SESSION)}
            for t in selection["tickers"] if t not in IWM_PICKS
        })
        result = fm.capture_session(
            SESSION, selection=selection, client=_client(wire, chain_store["cache"]),
            budget=fm.WireBudget.load(chain_store["forward"] / "budget" / "2026-10-05.json",
                                      cap=fm.DAILY_BUDGET_DEFAULT),
            now=NOW,
        )
        assert result.exit_code == 0
        assert result.wire_requests == len(selection["tickers"])
        doc = json.loads((chain_store["forward"] / "bars" / "2026-10-05.json").read_text())
        assert doc["schema"] == "desk-forward-minute-bars/1"
        assert doc["execution_authorized"] is False
        assert doc["selected_on"] == SELECT_ON.isoformat()
        assert doc["selection_source_sha256"] == selection["source_sha256"]
        assert set(doc["contracts"]) == set(selection["tickers"])
        iwm = doc["contracts"]["O:" + _occ("IWM", MONTHLY_NEAR, "C", 240)]
        assert iwm["status"] == "ok"
        assert iwm["dte_at_session"] == 11  # 2026-10-05 -> 2026-10-16
        assert iwm["dte_at_selection"] == 13
        assert len(iwm["bars"]) == 390 + 15  # 09:30..16:14 inclusive

    def test_second_run_is_pure_cache_and_does_not_rewrite(
        self, chain_store
    ) -> None:
        selection = fm.refresh_selection(selected_on=SELECT_ON)
        first = fm.capture_session(
            SESSION, selection=selection,
            client=_client(FakeWire(_full_bodies() | {
                t: {"status": "OK", "request_id": "q", "results": _session_bars(SESSION)}
                for t in selection["tickers"] if t not in IWM_PICKS
            }), chain_store["cache"]),
            budget=fm.WireBudget.load(chain_store["forward"] / "budget" / "2026-10-05.json",
                                      cap=100),
            now=NOW,
        )
        assert first.exit_code == 0
        written = (chain_store["forward"] / "bars" / "2026-10-05.json").read_bytes()
        wire = FakeWire({})
        result = fm.capture_session(
            SESSION, selection=selection, client=_client(wire, chain_store["cache"]),
            budget=fm.WireBudget.load(chain_store["forward"] / "budget" / "2026-10-05.json",
                                      cap=1),
            now=NOW,
        )
        assert result.exit_code == 0 and result.status == "exists"
        assert result.wire_requests == 0
        assert wire.urls == []
        assert (chain_store["forward"] / "bars" / "2026-10-05.json").read_bytes() == written

    def test_budget_stop_marks_the_rest_and_spends_no_wire(
        self, chain_store
    ) -> None:
        selection = fm.refresh_selection(selected_on=SELECT_ON)
        wire = FakeWire(_full_bodies())
        result = fm.capture_session(
            SESSION, selection=selection, client=_client(wire, chain_store["cache"]),
            budget=fm.WireBudget.load(chain_store["forward"] / "budget" / "2026-10-05.json",
                                      cap=3),
            now=NOW,
        )
        assert result.wire_requests == 3
        doc = json.loads((chain_store["forward"] / "bars" / "2026-10-05.json").read_text())
        statuses = [c["status"] for c in doc["contracts"].values()]
        assert statuses.count("ok") == 3
        assert statuses.count("budget") == len(selection["tickers"]) - 3
        ledger = json.loads(
            (chain_store["forward"] / "budget" / "2026-10-05.json").read_text())
        assert ledger["spent"] == 3
        assert len(ledger["refusals"]) == len(selection["tickers"]) - 3

    def test_a_stale_selection_refuses_before_any_wire(self, chain_store) -> None:
        stale_on = date(2026, 9, 18)  # 17 days before SESSION > MAX_SELECTION_AGE
        _write_chain(chain_store["store"], date(2026, 9, 17), "IWM", IWM_ROWS, 240.0)
        _write_chain(chain_store["store"], date(2026, 9, 17), "QQQ", QQQ_ROWS, 480.0)
        _write_chain(chain_store["store"], date(2026, 9, 17), "SPY", SPY_ROWS, 570.0)
        selection = fm.refresh_selection(selected_on=stale_on)
        wire = FakeWire(_full_bodies())
        result = fm.capture_session(
            SESSION, selection=selection, client=_client(wire, chain_store["cache"]),
            budget=fm.WireBudget.load(chain_store["forward"] / "budget" / "2026-10-05.json",
                                      cap=100),
            now=NOW,
        )
        assert result.exit_code == fm.STALE_SELECTION_EXIT
        assert wire.urls == []
        assert not (chain_store["forward"] / "bars" / "2026-10-05.json").exists()

    def test_selection_must_not_postdate_the_session(self, chain_store) -> None:
        selection = fm.refresh_selection(selected_on=SELECT_ON)
        result = fm.capture_session(
            date(2026, 10, 2), selection=selection,
            client=_client(FakeWire({}), chain_store["cache"]),
            budget=fm.WireBudget.load(chain_store["forward"] / "budget" / "d.json", cap=10),
            now=NOW,
        )
        assert result.exit_code == fm.STALE_SELECTION_EXIT

    def test_truncated_or_unusable_bars_are_flagged_not_swallowed(
        self, chain_store
    ) -> None:
        selection = fm.refresh_selection(selected_on=SELECT_ON)
        bodies = _full_bodies()
        ticker = sorted(IWM_PICKS)[0]
        bodies[ticker] = {"status": "OK", "request_id": "bad",
                          "results": [{"t": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 0}]}
        wire = FakeWire(bodies)
        result = fm.capture_session(
            SESSION, selection=selection, client=_client(wire, chain_store["cache"]),
            budget=fm.WireBudget.load(chain_store["forward"] / "budget" / "2026-10-05.json",
                                      cap=100),
            now=NOW,
        )
        assert result.exit_code == 0  # one bad contract must not lose the session
        doc = json.loads((chain_store["forward"] / "bars" / "2026-10-05.json").read_text())
        assert doc["contracts"][ticker]["status"] == "invalid"

    def test_dry_run_is_cache_only_and_writes_nothing(self, chain_store) -> None:
        selection = fm.refresh_selection(selected_on=SELECT_ON)
        wire = FakeWire(_full_bodies())
        result = fm.capture_session(
            SESSION, selection=selection, client=_client(wire, chain_store["cache"]),
            budget=fm.WireBudget.load(chain_store["forward"] / "budget" / "2026-10-05.json",
                                      cap=100),
            now=NOW, dry_run=True,
        )
        assert result.status == "dry-run"
        assert result.exit_code == 0
        assert result.wire_requests == 0
        assert wire.urls == []
        assert not (chain_store["forward"] / "bars" / "2026-10-05.json").exists()
        assert not (chain_store["forward"] / "budget" / "2026-10-05.json").exists()


# ------------------------------------------------------------ verify


class TestVerify:
    def test_full_coverage_verdict_ok(self, chain_store) -> None:
        selection = fm.refresh_selection(selected_on=SELECT_ON)
        fm.capture_session(
            SESSION, selection=selection,
            client=_client(FakeWire(_full_bodies()), chain_store["cache"]),
            budget=fm.WireBudget.load(chain_store["forward"] / "budget" / "d.json", cap=100),
            now=NOW,
        )
        verdict = fm.verify_session(SESSION)
        assert verdict.exit_code == 0
        doc = json.loads((chain_store["forward"] / "verify" / "2026-10-05.json").read_text())
        assert doc["schema"] == "desk-forward-verify/1"
        assert doc["clocks_et"] == ["10:00", "10:15", "15:15"]
        assert doc["ok"] is True
        ticker = "O:" + _occ("IWM", MONTHLY_NEAR, "C", 240)
        assert doc["contracts"][ticker]["missing"] == []

    def test_a_missing_decision_clock_fails_the_verdict(self, chain_store) -> None:
        selection = fm.refresh_selection(selected_on=SELECT_ON)
        bodies = {
            t: {"status": "OK", "request_id": "r", "results": _session_bars(SESSION, (15, 13))}
            for t in selection["tickers"]
        }
        fm.capture_session(
            SESSION, selection=selection,
            client=_client(FakeWire(bodies), chain_store["cache"]),
            budget=fm.WireBudget.load(chain_store["forward"] / "budget" / "d.json", cap=100),
            now=NOW,
        )
        verdict = fm.verify_session(SESSION)
        assert verdict.exit_code == fm.COVERAGE_EXIT
        doc = json.loads((chain_store["forward"] / "verify" / "2026-10-05.json").read_text())
        assert doc["ok"] is False
        assert doc["contracts"]["O:" + _occ("IWM", MONTHLY_NEAR, "C", 240)]["missing"] == ["15:15"]

    def test_a_contract_that_never_traded_is_reported_not_failed(self, chain_store) -> None:
        selection = fm.refresh_selection(selected_on=SELECT_ON)
        empty = {"status": "OK", "request_id": "e", "results": []}
        bodies = {t: empty for t in selection["tickers"]}
        fm.capture_session(
            SESSION, selection=selection,
            client=_client(FakeWire(bodies), chain_store["cache"]),
            budget=fm.WireBudget.load(chain_store["forward"] / "budget" / "d.json", cap=100),
            now=NOW,
        )
        verdict = fm.verify_session(SESSION)
        doc = json.loads((chain_store["forward"] / "verify" / "2026-10-05.json").read_text())
        assert doc["no_trade"] == sorted(selection["tickers"])
        assert verdict.exit_code == fm.COVERAGE_EXIT

    def test_no_bars_document_is_not_ok(self, chain_store) -> None:
        verdict = fm.verify_session(SESSION)
        assert verdict.exit_code == fm.COVERAGE_EXIT

    def test_clock_coverage_boundaries(self) -> None:
        def bar(hour: int, minute: int) -> dict:
            return {"t": int(datetime.combine(SESSION, time(hour, minute), tzinfo=ET)
                            .timestamp() * 1000), "v": 1}
        assert fm._clock_covered([bar(10, 0)], SESSION, "10:00") is True
        assert fm._clock_covered([bar(9, 59)], SESSION, "10:00") is False
        assert fm._clock_covered([bar(10, 1)], SESSION, "10:00") is False


# ------------------------------------------------------------ schedule


def _calendar_slots(unit: Path) -> list[tuple[frozenset[int], time]]:
    slots: list[tuple[frozenset[int], time]] = []
    for line in unit.read_text().splitlines():
        line = line.strip()
        if not line.startswith("OnCalendar="):
            continue
        m = _ONCAL.match(line)
        assert m is not None, f"unparsable OnCalendar line: {line!r}"
        spec = m.group("days")
        if spec == "*":
            days = frozenset(range(7))
        elif ".." in spec:
            lo, hi = (WEEKDAYS.index(x) for x in spec.split(".."))
            days = frozenset(range(lo, hi + 1))
        else:
            days = frozenset({WEEKDAYS.index(spec)})
        assert m.group("ss") == "00", line
        assert m.group("tz") == "America/New_York", line
        slots.append((days, time(int(m.group("hh")), int(m.group("mm")))))
    return slots


class TestTimerSchedule:
    def test_capture_slots_match_the_module_constants(self) -> None:
        slots = _calendar_slots(DEPLOY / "desk-forward-minutes.timer")
        assert slots, "desk-forward-minutes.timer has no OnCalendar line"
        rendered = {
            (frozenset(days), at) for days, at in (
                [fm.CAPTURE_DAYS, fm.CAPTURE_SLOT],
                [fm.CAPTURE_CATCHUP_DAYS, fm.CAPTURE_CATCHUP_SLOT],
            )
        }
        assert set(slots) == rendered

    def test_the_select_slot_matches_and_runs_after_the_saturday_chain(
        self,
    ) -> None:
        slots = _calendar_slots(DEPLOY / "desk-forward-select.timer")
        assert slots == [(frozenset({WEEKDAYS.index("Sat")}), fm.SELECT_SLOT)]
        chain = _calendar_slots(DEPLOY / "desk-chain.timer")
        sat_chain = [at for days, at in chain if WEEKDAYS.index("Sat") in days]
        assert sat_chain and max(at for at in sat_chain if at < fm.SELECT_SLOT), (
            "the Saturday selection must run after desk-chain's earliest Saturday slot"
        )

    def test_every_session_is_reachable_after_its_options_close(self) -> None:
        """The capture-window invariant: for any session D there is a firing
        strictly after D's 16:15 ET options close and within a day of it, so a
        missed evening slot never loses the session."""
        slots = _calendar_slots(DEPLOY / "desk-forward-minutes.timer")
        assert slots
        for session in (date(2026, 10, 6), date(2026, 10, 9), date(2026, 11, 26)):
            close = datetime.combine(session, time(16, 15), tzinfo=ET)
            fired = [
                datetime.combine(session + timedelta(days=k), at, tzinfo=ET)
                for k in (0, 1)
                for days, at in slots
                if (session + timedelta(days=k)).weekday() in days
            ]
            assert any(close < f <= close + timedelta(days=1) for f in fired), (
                f"no capture slot can reach {session}"
            )

    def test_services_are_oneshot_host_work_jobs_on_the_main_checkout(self) -> None:
        for unit, command in (
            ("desk-forward-select.service", fm.SELECT_COMMAND),
            ("desk-forward-minutes.service", fm.CAPTURE_COMMAND),
        ):
            text = (DEPLOY / unit).read_text()
            assert "Type=oneshot" in text, unit
            assert "Slice=host-work.slice" in text, unit
            assert f"-m tree_options.desk {command}" in text, unit
            assert "PYTHONPATH=/home/alexk/documents/tree_options/src" in text, unit

    def test_decision_clocks_stay_pinned_to_the_cost_module(self) -> None:
        outcomes = (Path(__file__).resolve().parents[2]
                    / "src/tree_options/desk/outcomes.py").read_text()
        assert 'decision_clocks_et=("10:00", "10:15", "15:15")' in outcomes, (
            "outcomes.py's decision clocks moved: forward_minutes must be revisited"
        )
        assert fm.DECISION_CLOCKS == ("10:00", "10:15", "15:15")


# ------------------------------------------------------------ CLI


class TestCli:
    def test_forward_minutes_and_verify_through_run_cli(
        self, chain_store, static_calendar
    ) -> None:
        assert run_cli(["forward-select", "--selected-on", str(SELECT_ON)],
                       cal=static_calendar) == 0
        selection = chain_store["forward"] / "selection" / "2026-10-03.json"
        assert selection.exists()
        wire = FakeWire(_full_bodies())
        rc = run_cli(
            ["forward-minutes", "--session", str(SESSION),
             "--massive-cache", str(chain_store["cache"]), "--budget", "100"],
            forward_client=_client(wire, chain_store["cache"]),
            now=NOW,
            cal=static_calendar,
        )
        assert rc == 0
        assert (chain_store["forward"] / "bars" / "2026-10-05.json").exists()
        assert run_cli(["forward-verify", "--session", str(SESSION)], now=NOW,
                       cal=static_calendar) == 0
        assert KEY.encode() not in (
            chain_store["forward"] / "bars" / "2026-10-05.json").read_bytes()

    def test_forward_verify_without_bars_reports_coverage_exit(
        self, chain_store, static_calendar
    ) -> None:
        rc = run_cli(["forward-verify", "--session", str(SESSION)], now=NOW,
                     cal=static_calendar)
        assert rc == fm.COVERAGE_EXIT


class TestCliExitMapping:
    """The runbook's exit-2/3 contract at the CLI boundary: a tier boundary
    is a purchase decision (exit 2), a dead key says rotate (exit 3), and
    NEITHER is an unhandled traceback (the 2026-10-01 first fire died at
    exit 1 with a stack)."""

    def _run(self, monkeypatch, exc, static_calendar):
        from tree_options.desk import __main__ as main_mod

        monkeypatch.setattr(
            main_mod.forward_minutes, "capture_session",
            lambda *a, **k: (_ for _ in ()).throw(exc),
        )
        monkeypatch.setattr(
            main_mod.forward_minutes, "latest_selection",
            lambda: {"schema": "desk-forward-selection/1", "contracts": []},
        )
        args = argparse.Namespace(
            session=None, selection=None, budget=None, dry_run=True,
            massive_cache=None,
        )
        captured = {}

        def fake_print(*a, **k):
            captured["out"] = captured.get("out", "") + " ".join(map(str, a))

        monkeypatch.setattr(main_mod.sys, "stderr", _Sink(fake_print))
        rc = main_mod._forward_minutes(args, client=object(), clock=lambda: NOW, cal=static_calendar)
        return rc, captured.get("out", "")

    def test_not_entitled_maps_to_exit_2_purchase_decision(self, monkeypatch, static_calendar):
        from tree_options.data.massive_client import MassiveNotEntitledError

        rc, out = self._run(
            monkeypatch,
            MassiveNotEntitledError("/v2/aggs/...", "plan doesn't include this data"),
            static_calendar,
        )
        assert rc == 2
        assert "NOT_ENTITLED" in out and "purchase decision" in out

    def test_auth_rejected_maps_to_exit_3_rotate(self, monkeypatch, static_calendar):
        from tree_options.data.massive_client import MassiveAuthRejectedError

        rc, out = self._run(
            monkeypatch,
            MassiveAuthRejectedError("/v2/aggs/...", 403, "key rejected"),
            static_calendar,
        )
        assert rc == 3
        assert "rotate the key" in out


class _Sink:
    def __init__(self, write): self._write = write
    def write(self, s): self._write(s)
    def flush(self): pass
