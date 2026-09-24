"""Recorded options-surface endpoint: ATM window math, expiry cap, walk-back
to a matching chain, cards-only degrade, iv30 decimation with stale-last
disclosure — plus the Phase 4 live viewchain section — the JSON contract of
/api/market/{sym}/options."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from tree_options.desk.store import ChainStore, encode_document
from tree_options.trex.discovery.market import MarketCache
from tree_options.trex_web.app import create_app
from tree_options.trex_web.options_view import clamp_params, options_payload
from tree_options.trex_web.symbol_history import _ts_ms

SYM = "TEST"
SESSION = "2026-06-11"
STRIKES = [80.0, 85.0, 90.0, 95.0, 100.0, 105.0, 110.0, 115.0, 120.0]
EXPIRIES = [
    "2026-06-12",
    "2026-06-19",
    "2026-07-17",
    "2026-09-18",
    "2026-12-18",
    "2027-01-15",
    "2027-06-18",
    "2027-12-17",
]
IV_LAST_DAY = "2026-06-03"  # the iv-history file trails the chain store
IV_DAYS = 900

CHAIN_FIELDS = (
    "occ",
    "exp",
    "right",
    "strike",
    "bid",
    "ask",
    "iv",
    "delta",
    "gamma",
    "theta",
    "vega",
    "rho",
    "theo",
    "oi",
    "volume",
    "last",
    "last_time",
)


def _feature_name(spot: float | None) -> dict[str, Any]:
    return {
        "spot": spot,
        "rate": 0.0401,
        "atm_term": [["2026-06-19", 8, 0.2512, 9, "bracket"]],
        "iv": {"30": 0.25, "60": 0.26, "90": 0.27, "180": 0.28},
        "skew25": {"30": 0.011, "90": 0.023},
        "term_slope": 0.111,
        "liquidity_score": 85,
        "earnings": {
            "next_report": "2026-08-06",
            "event_sessions": ["2026-08-06", "2026-08-07"],
            "implied_move": 0.031,
            "hist_mean_abs_move": 0.042,
            "hist_n": 8,
            "in_progress": False,
        },
        "iv_rank": {
            "rank": 0.369,
            "percentile": 0.19,
            "n": 220,
            "low_n": False,
            "outside_range": None,
        },
        "yz22_ann": 0.233,
        "forecast": {"source": "har"},
    }


def _write_features(store: Path, session: str, names: dict[str, Any]) -> None:
    features = store / "features"
    features.mkdir(parents=True, exist_ok=True)
    (features / f"{session}.json").write_text(
        json.dumps(
            {
                "schema": "desk-features/1",
                "session": session,
                "names": names,
                "warnings": [],
            }
        )
    )


def _write_chain(
    store: Path,
    session: str,
    sym: str,
    expiries: list[str],
    strikes: list[float],
    *,
    close: float = 100.0,
    one_sided: tuple[str, str, float] | None = None,
    null_iv: tuple[str, str, float] | None = None,
) -> None:
    cols: dict[str, list[Any]] = {f: [] for f in CHAIN_FIELDS}
    for exp in expiries:
        for right in ("C", "P"):
            for strike in strikes:
                key = (exp, right, strike)
                bid = 5.0 + strike / 100.0
                cols["occ"].append(
                    f"{sym}{exp.replace('-', '')[2:]}{right}{int(strike * 1000):08d}"
                )
                cols["exp"].append(exp)
                cols["right"].append(right)
                cols["strike"].append(strike)
                cols["bid"].append(bid)
                cols["ask"].append(None if key == one_sided else bid + 0.5)
                cols["iv"].append(None if key == null_iv else 0.25)
                cols["delta"].append(0.5)
                cols["gamma"].append(0.01)
                cols["theta"].append(-0.02)
                cols["vega"].append(0.03)
                cols["rho"].append(0.004)
                cols["theo"].append(bid + 0.25)
                cols["oi"].append(100)
                cols["volume"].append(10)
                cols["last"].append(bid + 0.4)
                cols["last_time"].append(f"{session}T15:58:00-04:00")
    doc = {
        "header": {
            "schema": "desk-chain/1",
            "session": session,
            "underlying": sym,
            "underlying_quote": {
                "close": close,
                "last_trade_time": f"{session}T15:59:59-04:00",
            },
            "n": len(cols["exp"]),
        },
        "columns": cols,
    }
    path = ChainStore(store).chain_path(date.fromisoformat(session), sym)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode_document(doc))


def _iv30_days() -> list[str]:
    end = date.fromisoformat(IV_LAST_DAY)
    return [(end - timedelta(days=IV_DAYS - 1 - i)).isoformat() for i in range(IV_DAYS)]


def _write_iv_history(store: Path, sym: str) -> None:
    iv_dir = store / "iv-history"
    iv_dir.mkdir(parents=True, exist_ok=True)
    (iv_dir / "vwap_atm.json").write_text(
        json.dumps(
            {
                "schema": "ivhist/1",
                "names": {
                    sym: {
                        "sessions": {
                            d: {"iv30": 0.2 + i * 1e-4, "spot": 100.0, "method": "vwap"}
                            for i, d in enumerate(_iv30_days())
                        }
                    }
                },
            }
        )
    )


def _client(store: Path, discovery: Path | None = None) -> TestClient:
    app = create_app(
        state_dir=str(store.parent),
        plans_dir=str(store.parent),
        desk_store_dir=str(store),
        discovery_dir=str(discovery or store.parent / "disc"),
    )
    return TestClient(app)


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    store = tmp_path / "store"
    _write_features(store, SESSION, {"TEST": _feature_name(101.0), "OTHR": _feature_name(50.0)})
    _write_chain(
        store,
        SESSION,
        SYM,
        EXPIRIES,
        STRIKES,
        one_sided=("2026-06-12", "C", 95.0),
        null_iv=("2026-06-12", "P", 105.0),
    )
    _write_iv_history(store, SYM)
    return _client(store)


def _get(client: TestClient, path: str) -> dict[str, Any]:
    r = client.get(path)
    assert r.status_code == 200
    return r.json()


def test_contract_and_cards(client: TestClient) -> None:
    body = _get(client, f"/api/market/{SYM}/options")
    assert set(body) == {
        "now",
        "symbol",
        "recorded",
        "iv30_history",
        "live",
        "available",
        "warnings",
    }
    assert body["symbol"] == SYM
    assert body["live"] is None  # Phase 4's slot, present for contract stability
    assert body["available"] is True
    assert body["warnings"] == []
    rec = body["recorded"]
    assert rec["session"] == SESSION
    assert isinstance(rec["age_seconds"], float) and rec["age_seconds"] > 0.0
    assert rec["spot"] == 101.0
    assert set(rec) == {"session", "age_seconds", "spot", "cards", "atm_term", "slice"}
    cards = rec["cards"]
    assert set(cards) == {
        "iv",
        "iv_rank",
        "skew25",
        "term_slope",
        "yz22_ann",
        "liquidity_score",
        "earnings",
    }
    assert cards["iv"]["30"] == 0.25
    assert cards["iv_rank"]["rank"] == 0.369
    assert cards["liquidity_score"] == 85
    assert cards["earnings"]["next_report"] == "2026-08-06"
    assert rec["atm_term"] == [["2026-06-19", 8, 0.2512, 9, "bracket"]]


def test_atm_window_math(client: TestClient) -> None:
    body = _get(client, f"/api/market/{SYM}/options?window=1&max_expiries=1")
    rows = body["recorded"]["slice"]
    assert len(rows) == 6  # 95/100/105 x C/P — spot 101 puts ATM at 100
    assert {r["strike"] for r in rows} == {95.0, 100.0, 105.0}
    assert {r["right"] for r in rows} == {"C", "P"}
    assert sorted(r["atm"] for r in rows) == [False, False, False, False, True, True]
    assert all(r["dte"] == 1 and r["exp"] == "2026-06-12" for r in rows)
    assert [(r["right"], r["strike"]) for r in rows] == [
        ("C", 95.0),
        ("C", 100.0),
        ("C", 105.0),
        ("P", 95.0),
        ("P", 100.0),
        ("P", 105.0),
    ]
    # one-sided quote -> mid null, but bid still served; greek null passes through
    one = next(r for r in rows if (r["right"], r["strike"]) == ("C", 95.0))
    assert one["ask"] is None and one["mid"] is None and one["bid"] is not None
    null_iv = next(r for r in rows if (r["right"], r["strike"]) == ("P", 105.0))
    assert null_iv["iv"] is None
    both = next(r for r in rows if (r["right"], r["strike"]) == ("C", 100.0))
    assert both["mid"] == (both["bid"] + both["ask"]) / 2
    assert both["oi"] == 100 and both["volume"] == 10


def test_expiry_cap_and_param_clamping(client: TestClient) -> None:
    def slice_of(q: str) -> list[dict[str, Any]]:
        return _get(client, f"/api/market/{SYM}/options{q}")["recorded"]["slice"]

    def exps(rows: list[dict[str, Any]]) -> list[str]:
        return sorted({r["exp"] for r in rows})

    # default max_expiries=6 of the 8 recorded
    assert exps(slice_of("?window=0")) == EXPIRIES[:6]
    assert exps(slice_of("?window=0&max_expiries=2")) == EXPIRIES[:2]
    assert exps(slice_of("?window=0&max_expiries=0")) == EXPIRIES[:1]  # clamp to 1
    assert exps(slice_of("?window=0&max_expiries=99")) == EXPIRIES  # clamp to 12
    # window clamps: 0 -> ATM only; huge -> whole ladder (9 strikes)
    assert len(slice_of("?window=0&max_expiries=1")) == 2
    assert len(slice_of("?window=99&max_expiries=1")) == 18
    assert len(slice_of("?window=-5&max_expiries=1")) == 2  # clamp to 0
    # clamp_params pinned directly at the boundaries
    assert clamp_params(-5, 0) == (0, 1)
    assert clamp_params(5, 6) == (5, 6)
    assert clamp_params(15, 12) == (15, 12)
    assert clamp_params(99, 99) == (15, 12)


def test_walkback_when_newest_features_lacks_chain(
    client: TestClient, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    # a NEWER features session carries TEST but no chain was recorded for it
    _write_features(store, "2026-06-12", {"TEST": _feature_name(101.5)})
    body = _get(client, f"/api/market/{SYM}/options?window=0")
    rec = body["recorded"]
    assert rec["session"] == SESSION  # lockstep: features+chain from the same D
    assert rec["slice"] is not None
    assert any("serving TEST from session 2026-06-11" in w for w in body["warnings"])


def test_cards_only_when_no_chain_at_all(client: TestClient) -> None:
    # OTHR is in features but no chain was ever recorded for it
    body = _get(client, "/api/market/OTHR/options")
    assert body["available"] is True
    rec = body["recorded"]
    assert rec["session"] == SESSION
    assert rec["spot"] == 50.0
    assert rec["cards"]["iv"]["30"] == 0.25
    assert rec["slice"] is None
    assert any("cards only" in w for w in body["warnings"])
    assert body["iv30_history"] is None  # no entry for the name either


def test_spot_falls_back_to_chain_header_close(tmp_path: Path) -> None:
    store = tmp_path / "store2"
    _write_features(store, SESSION, {"TEST": _feature_name(None)})
    _write_chain(store, SESSION, SYM, ["2026-06-19"], [95.0, 100.0, 105.0], close=99.0)
    body = _get(_client(store), f"/api/market/{SYM}/options?window=0")
    rec = body["recorded"]
    assert rec["spot"] == 99.0  # features spot null -> chain underlying close
    rows = rec["slice"]
    assert {r["strike"] for r in rows} == {100.0}  # nearest to 99
    assert body["iv30_history"] is None  # no iv-history file in this store
    assert body["warnings"] == []


def test_unknown_name_available_false(client: TestClient) -> None:
    body = _get(client, "/api/market/NOPEF/options")
    assert body["available"] is False
    assert body["recorded"] is None
    assert body["iv30_history"] is None
    assert body["live"] is None
    assert body["warnings"] == []


def test_bad_symbol_404_and_case_normalize(client: TestClient) -> None:
    assert client.get("/api/market/TOOLONGSYM/options").status_code == 404
    assert client.get("/api/market/aa-pl1/options").status_code == 404
    assert _get(client, f"/api/market/{SYM.lower()}/options")["symbol"] == SYM


def test_iv30_decimation_and_stale_last(client: TestClient) -> None:
    hist = _get(client, f"/api/market/{SYM}/options")["iv30_history"]
    assert hist is not None
    days = _iv30_days()
    assert hist["n"] == IV_DAYS  # pre-decimation session count
    assert len(hist["points"]) <= 400  # decimate_pairs cap
    pts = hist["points"]
    assert pts[0] == [_ts_ms(days[0]), 0.2]  # first/last kept exactly
    assert pts[-1] == [_ts_ms(days[-1]), 0.2 + (IV_DAYS - 1) * 1e-4]
    assert hist["first"] == days[0]
    assert hist["last"] == IV_LAST_DAY  # trails the chain session; disclosed
    assert hist["last"] != SESSION  # not "fixed" to look fresher
    assert hist["y_lo"] <= 0.2 and hist["y_hi"] >= 0.2 + (IV_DAYS - 1) * 1e-4
    assert "IVHIST-001" in hist["source"]


def test_missing_store_degrades_to_unavailable(tmp_path: Path) -> None:
    body = _get(_client(tmp_path / "nothing"), f"/api/market/{SYM}/options")
    assert body["available"] is False
    assert body["recorded"] is None
    assert body["iv30_history"] is None
    assert body["warnings"] == []


# ------------------------------------------------------- live (Phase 4)

ET = ZoneInfo("America/New_York")
LIVE_NOW = datetime(2026, 9, 24, 15, 0, tzinfo=ET)
LIVE_SPOT = 101.5
LIVE_STRIKES = (85.0, 90.0, 95.0, 100.0, 105.0, 110.0, 115.0)
LIVE_EXPIRIES = ("20261016", "20261218", "20270319")


def _viewchain_payload(spot: float | None = LIVE_SPOT) -> dict[str, Any]:
    """A viewchain envelope payload as market_cycle would cache it (strike
    keys serialize to strings on disk — the reader must coerce them back)."""
    expiries: dict[str, Any] = {}
    for exp in LIVE_EXPIRIES:
        expiries[exp] = {
            right: {
                strike: {
                    "bid": round(strike / 200.0, 3),
                    "ask": round(strike / 200.0 + 0.25, 3),
                    "iv": 0.32,
                    "delta": 0.5 if right == "C" else -0.5,
                    "volume": 7,
                    "open_interest": 70,
                }
                for strike in LIVE_STRIKES
            }
            for right in ("C", "P")
        }
    return {"spot": spot, "expiries": expiries}


def _write_viewchain(discovery: Path, sym: str, payload: dict[str, Any], now: datetime) -> None:
    MarketCache(discovery / "market" / "cache").put("viewchain", sym, payload, now=now)


def test_live_section_from_viewchain_envelope(tmp_path: Path) -> None:
    discovery = tmp_path / "disc"
    fetched = LIVE_NOW - timedelta(seconds=90)
    _write_viewchain(discovery, SYM, _viewchain_payload(), fetched)
    body = options_payload(
        tmp_path / "store",
        SYM,
        1,
        2,
        LIVE_NOW,
        market_cache_dir=discovery / "market" / "cache",
    )
    live = body["live"]
    assert live is not None
    assert set(live) == {"fetched_at", "age_seconds", "ttl_seconds", "spot", "slice"}
    assert live["fetched_at"] == fetched.isoformat()
    assert live["age_seconds"] == 90  # envelope fetched_at vs now
    assert live["ttl_seconds"] == 300
    assert live["spot"] == LIVE_SPOT
    rows = live["slice"]
    # 2 nearest expiries x (95/100/105 x C/P) — spot 101.5 puts ATM at 100
    assert sorted({r["exp"] for r in rows}) == ["2026-10-16", "2026-12-18"]
    assert len(rows) == 12
    assert rows[0] == {
        "exp": "2026-10-16",
        "dte": 22,  # expiry minus today (2026-09-24), session-date arithmetic
        "right": "C",
        "strike": 95.0,
        "atm": False,
        "bid": 0.475,
        "ask": 0.725,
        "mid": 0.6,
        "iv": 0.32,
        "delta": 0.5,
        "gamma": None,
        "theta": None,
        "vega": None,
        "oi": 70,  # served from open_interest
        "volume": 7,
    }
    assert {(r["exp"], r["right"], r["strike"]) for r in rows if r["atm"]} == {
        ("2026-10-16", "C", 100.0),
        ("2026-10-16", "P", 100.0),
        ("2026-12-18", "C", 100.0),
        ("2026-12-18", "P", 100.0),
    }


def test_live_counts_coerce_cboe_floats(tmp_path: Path) -> None:
    # the wire sends volume/open_interest as floats (5915.0) — the row
    # contract wants ints, and 0.0 must survive as 0 (not null)
    discovery = tmp_path / "disc"
    payload = {
        "spot": 100.0,
        "expiries": {
            "20261016": {
                "C": {
                    "100.0": {
                        "bid": 1.0,
                        "ask": 1.5,
                        "iv": 0.3,
                        "delta": 0.5,
                        "volume": 5915.0,
                        "open_interest": 0.0,
                    }
                }
            }
        },
    }
    _write_viewchain(discovery, SYM, payload, LIVE_NOW)
    body = options_payload(
        tmp_path / "store", SYM, 0, 1, LIVE_NOW, market_cache_dir=discovery / "market" / "cache"
    )
    row = body["live"]["slice"][0]
    assert row["volume"] == 5915
    assert row["oi"] == 0


def test_live_missing_envelope_stays_null(tmp_path: Path) -> None:
    body = options_payload(
        tmp_path / "store",
        SYM,
        1,
        2,
        LIVE_NOW,
        market_cache_dir=tmp_path / "disc" / "market" / "cache",
    )
    assert body["live"] is None


def test_live_without_spot_windows_around_median_rung(tmp_path: Path) -> None:
    discovery = tmp_path / "disc"
    _write_viewchain(discovery, SYM, _viewchain_payload(spot=None), LIVE_NOW)
    body = options_payload(
        tmp_path / "store", SYM, 0, 6, LIVE_NOW, market_cache_dir=discovery / "market" / "cache"
    )
    live = body["live"]
    assert live is not None and live["spot"] is None
    # median rung of the 85..115 ladder is 100 -> ATM flags mark it
    assert {(r["right"], r["strike"]) for r in live["slice"]} == {("C", 100.0), ("P", 100.0)}
    assert all(r["atm"] for r in live["slice"])


def test_live_served_through_app_wiring(tmp_path: Path) -> None:
    store = tmp_path / "store"
    discovery = tmp_path / "disc"
    _write_features(store, SESSION, {"TEST": _feature_name(101.0)})
    _write_viewchain(discovery, SYM, _viewchain_payload(), LIVE_NOW)
    live = _get(_client(store, discovery), f"/api/market/{SYM}/options?window=0&max_expiries=1")[
        "live"
    ]
    assert live is not None
    assert live["spot"] == LIVE_SPOT  # discovered from discovery_dir's market/cache
    assert {r["strike"] for r in live["slice"]} == {100.0}
    assert live["age_seconds"] is not None


def test_market_cache_dir_param_overrides_discovery_root(tmp_path: Path) -> None:
    store = tmp_path / "store"
    discovery = tmp_path / "disc"
    _write_features(store, SESSION, {"TEST": _feature_name(101.0)})
    _write_viewchain(discovery, SYM, _viewchain_payload(), LIVE_NOW)
    app = create_app(
        state_dir=str(store.parent),
        plans_dir=str(store.parent),
        desk_store_dir=str(store),
        discovery_dir=str(discovery),
        market_cache_dir=str(tmp_path / "elsewhere" / "market" / "cache"),
    )
    assert _get(TestClient(app), f"/api/market/{SYM}/options")["live"] is None
