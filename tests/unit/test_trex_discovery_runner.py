"""C8: the discovery runner — artifact writes, serve tick, flock (fakes)."""

from __future__ import annotations

import fcntl
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tree_options.trex.account import AccountSnapshot
from tree_options.trex.discovery.artifact import (
    read_latest,
    read_scan_result,
    write_scan_request,
)
from tree_options.trex.discovery.config import ScanConfig
from tree_options.trex.discovery.engine import ChainRow
from tree_options.trex.discovery.runner import (
    ChainUnavailable,
    ensure_lock,
    run_once,
    serve_tick,
)

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 22, 16, 5, tzinfo=ET)


def _rows() -> list[ChainRow]:
    return [
        ChainRow(strike=140.0, bid=0.10, ask=0.14, delta=None, ts=NOW),
        ChainRow(strike=145.0, bid=0.16, ask=0.20, delta=None, ts=NOW),
        ChainRow(strike=150.0, bid=0.40, ask=0.46, delta=None, ts=NOW),
        ChainRow(strike=155.0, bid=0.90, ask=1.00, delta=None, ts=NOW),
        ChainRow(strike=160.0, bid=1.70, ask=1.85, delta=None, ts=NOW),
    ]


class FakeChainSource:
    def __init__(self, fail: str | None = None, account: AccountSnapshot | None = None) -> None:
        self.fail = fail
        self._account = account
        self.cancelled = 0

    def chain(self, symbol: str) -> tuple[list[str], list[float]] | None:
        if self.fail == symbol:
            return None
        return (
            ["20261016", "20261120"],
            [140.0, 145.0, 150.0, 155.0, 160.0],
        )

    def put_rows(self, symbol: str, expiry: str, strikes: list[float], limit: int) -> list[ChainRow]:
        return [r for r in _rows() if r.strike in strikes][:limit]

    def account(self) -> AccountSnapshot | None:
        return self._account

    def cancel_all(self) -> None:
        self.cancelled += 1


def _cfg(**over: object) -> ScanConfig:
    fields: dict = {"underlyings": ["NVDA", "QQQ"], "widths": [5.0, 10.0]}
    fields.update(over)
    return ScanConfig(**fields)  # type: ignore[arg-type]


def _account() -> AccountSnapshot:
    return AccountSnapshot(
        account_id="DUT143714",
        net_liquidation=Decimal("1000252.09"),
        cash=Decimal("999516.91"),
        buying_power=Decimal("3998067.63"),
        currency="USD",
        ts=NOW,
    )


class TestRunOnce:
    def test_writes_artifact_from_source_rows(self, tmp_path: Path) -> None:
        state = tmp_path / "state"
        path = run_once(FakeChainSource(account=_account()), _cfg(), state, "manual", repo=None, now=NOW)
        doc = read_latest(state)
        assert doc is not None
        assert doc["payload"]["data_quality"]["underlyings_scanned"] == 2
        assert doc["payload"]["data_quality"]["chains_available"] is True
        assert doc["stamp"]["runner"] == "manual"
        # per-underlying candidates present, ranked
        top = doc["payload"]["candidates"][0]
        assert top["rank"] == 1
        # account copied into the discovery state dir (freshest-wins reader)
        acct = json.loads((state / "account.json").read_text())
        assert acct["account_id"] == "DUT143714"
        assert path.exists()

    def test_chain_failure_is_disclosed_not_fatal(self, tmp_path: Path) -> None:
        state = tmp_path / "state"
        run_once(FakeChainSource(fail="QQQ"), _cfg(), state, "manual", repo=None, now=NOW)
        doc = read_latest(state)
        assert doc is not None
        dq = doc["payload"]["data_quality"]
        assert dq["underlyings_requested"] == 2
        assert dq["underlyings_scanned"] == 1
        assert dq["chains_available"] is False
        assert "QQQ chain unavailable" in dq["notes"]

    def test_mode_recorded_in_payload(self, tmp_path: Path) -> None:
        run_once(FakeChainSource(), _cfg(), tmp_path / "s", "auto", repo=None, now=NOW)
        doc = read_latest(tmp_path / "s")
        assert doc is not None
        assert doc["payload"]["mode"] == "auto"

    def test_no_greeks_note_present(self, tmp_path: Path) -> None:
        run_once(FakeChainSource(), _cfg(), tmp_path / "s", "manual", repo=None, now=NOW)
        doc = read_latest(tmp_path / "s")
        assert doc is not None
        assert any("no greeks" in n for n in doc["payload"]["data_quality"]["notes"])


class TestServeTick:
    def test_consumes_pending_request_end_to_end(self, tmp_path: Path) -> None:
        state = tmp_path / "state"
        spool = state / "spool"
        write_scan_request(spool, "req-77", NOW)
        done = serve_tick(FakeChainSource(), _cfg(), state, now=NOW)
        assert done is True
        result = read_scan_result(spool)
        assert result is not None
        assert result["request_id"] == "req-77"
        assert result["status"] == "ok"
        doc = read_latest(state)
        assert doc is not None
        assert doc["payload"]["mode"] == "manual"

    def test_no_request_and_no_auto_time_means_no_scan(self, tmp_path: Path) -> None:
        state = tmp_path / "state"
        done = serve_tick(FakeChainSource(), _cfg(), state, now=NOW)
        assert done is False
        assert read_latest(state) is None

    def test_auto_time_triggers_a_scan(self, tmp_path: Path) -> None:
        state = tmp_path / "state"
        at_auto = datetime(2026, 9, 22, 16, 11, tzinfo=ET)
        done = serve_tick(FakeChainSource(), _cfg(), state, now=at_auto)
        assert done is True
        doc = read_latest(state)
        assert doc is not None
        assert doc["payload"]["mode"] == "auto"

    def test_failure_result_is_recorded(self, tmp_path: Path) -> None:
        state = tmp_path / "state"
        spool = state / "spool"
        write_scan_request(spool, "req-9", NOW)
        serve_tick(ChainFailingSource(), _cfg(), state, now=NOW)
        result = read_scan_result(spool)
        assert result is not None
        assert result["status"] == "error"


class ChainFailingSource:
    def chain(self, symbol: str):
        return (
            ["20261016", "20261120"],
            [140.0, 145.0, 150.0, 155.0, 160.0],
        )

    def put_rows(self, symbol: str, expiry: str, strikes: list[float], limit: int):
        raise ChainUnavailable("row subscription exploded")

    def account(self) -> AccountSnapshot | None:
        return None

    def cancel_all(self) -> None:
        raise ChainUnavailable("cancel exploded")


class TestLock:
    def test_double_runner_refused_by_flock(self, tmp_path: Path) -> None:
        lock = tmp_path / "discovery.lock"
        first = ensure_lock(lock)
        assert first is not None
        with pytest.raises(OSError):
            ensure_lock(lock)
        fcntl.flock(first, fcntl.LOCK_UN)


class TestAccountHistory:
    """M1: the discovery serve loop owns account equity history (a
    per-plan monitor dies with its book; discovery runs continuously)."""

    def _acct(self, ts: datetime, nlv: str = "1000252.09") -> AccountSnapshot:
        return AccountSnapshot(
            account_id="DUT143714",
            net_liquidation=Decimal(nlv),
            cash=Decimal("999516.91"),
            buying_power=Decimal("3998067.63"),
            currency="USD",
            ts=ts,
        )

    def _path(self, state: Path) -> Path:
        return state / "account_history.jsonl"

    def test_appends_when_stale_and_skips_when_fresh(self, tmp_path: Path) -> None:
        from tree_options.trex.discovery.runner import ACCOUNT_HISTORY_TTL_SECONDS

        state = tmp_path / "state"
        src = FakeChainSource(account=self._acct(NOW))
        serve_tick(src, _cfg(), state, now=NOW)
        lines = self._path(state).read_text().splitlines()
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["account_id"] == "DUT143714"
        assert row["net_liquidation"] == "1000252.09"  # strings: book-lane money
        assert row["source"] == "discovery"
        # within the TTL: no second line
        fresh = NOW + timedelta(seconds=ACCOUNT_HISTORY_TTL_SECONDS - 5)
        serve_tick(src, _cfg(), state, now=fresh)
        assert len(self._path(state).read_text().splitlines()) == 1
        # past the TTL: appends again
        stale = NOW + timedelta(seconds=ACCOUNT_HISTORY_TTL_SECONDS + 5)
        serve_tick(src, _cfg(), state, now=stale)
        assert len(self._path(state).read_text().splitlines()) == 2

    def test_account_failure_is_isolated(self, tmp_path: Path) -> None:
        state = tmp_path / "state"

        class AccountRaising(FakeChainSource):
            def account(self) -> AccountSnapshot | None:
                raise RuntimeError("gateway hiccup")

        # no exception escapes; the rest of the tick still runs
        assert serve_tick(AccountRaising(), _cfg(), state, now=NOW) is False
        assert not self._path(state).exists()

    def test_none_account_writes_nothing(self, tmp_path: Path) -> None:
        state = tmp_path / "state"
        serve_tick(FakeChainSource(account=None), _cfg(), state, now=NOW)
        assert not self._path(state).exists()


class TestShadowHook:
    """M3: serve_tick opens + marks shadow alternatives after each scan."""

    def test_scan_creates_shadow_book(self, tmp_path: Path) -> None:
        state = tmp_path / "state"
        spool = state / "spool"
        write_scan_request(spool, "req-s1", NOW)
        serve_tick(FakeChainSource(), _cfg(), state, now=NOW)
        book = (state / "shadow_book.json")
        assert book.exists()
        doc = json.loads(book.read_text())
        assert doc["version"] == 1
        assert doc["positions"], "fake scan should accept at least one candidate"
        pos = doc["positions"][0]
        assert pos["status"] == "open"
        assert pos["qty"] == 1
        assert pos["debit_paid"] > 0
        assert (state / "shadow_marks.jsonl").exists()

    def test_shadow_failure_never_fails_the_scan(self, tmp_path: Path) -> None:
        state = tmp_path / "state"
        spool = state / "spool"
        write_scan_request(spool, "req-s2", NOW)
        import tree_options.trex.discovery.runner as runner_mod

        original = runner_mod._post_scan_shadow
        runner_mod._post_scan_shadow = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("shadow exploded")
        )
        try:
            done = serve_tick(FakeChainSource(), _cfg(), state, now=NOW)
        finally:
            runner_mod._post_scan_shadow = original
        assert done is True
        result = read_scan_result(spool)
        assert result is not None and result["status"] == "ok"


class TestMarketTick:
    """M5a: serve_tick drives the TTL-gated market refresh (with an
    injected transport; None skips the wire)."""

    def _transport(self, body: bytes):
        def t(url: str, *, timeout: float = 10.0):
            return 200, body

        return t

    def test_market_cycle_writes_market_json(self, tmp_path: Path) -> None:
        import json as _json

        state = tmp_path / "state"
        body = _json.dumps(
            {
                "timestamp": "2026-09-22 22:08:55",
                "data": {"bid": 1.0, "ask": 1.1, "close": 1.05, "iv30": 12.0,
                          "price_change_percent": 0.1},
            }
        ).encode()
        serve_tick(
            FakeChainSource(), _cfg(), state, now=NOW, market_transport=self._transport(body)
        )
        doc = _json.loads((state / "market.json").read_text())
        assert "NVDA" in doc["symbols"] and "QQQ" in doc["symbols"]

    def test_no_transport_means_no_market_work(self, tmp_path: Path) -> None:
        state = tmp_path / "state"
        serve_tick(FakeChainSource(), _cfg(), state, now=NOW)
        assert not (state / "market.json").exists()

    def test_force_request_round_trip(self, tmp_path: Path) -> None:
        import json as _json

        from tree_options.trex.discovery.artifact import (
            read_result,
            write_request,
        )

        state = tmp_path / "state"
        body = _json.dumps(
            {"timestamp": "t", "data": {"bid": 1.0, "ask": 1.1, "close": 1.0}}
        ).encode()
        write_request(
            state / "spool", "market", "req-m1", {"request_ts": NOW.isoformat()}
        )
        serve_tick(
            FakeChainSource(), _cfg(), state, now=NOW, market_transport=self._transport(body)
        )
        result = read_result(state / "spool", "market")
        assert result is not None and result["status"] == "ok"


class TestBrokerDown:
    """Codex-arch #2: a dead gateway degrades the loop to market/watch
    work; pending scan claims complete with an error receipt instead of
    hanging, and nothing broker-side runs."""

    def _transport(self, body: bytes):
        def t(url: str, *, timeout: float = 10.0):
            return 200, body

        return t

    def test_scan_claim_errors_market_survives(self, tmp_path: Path) -> None:
        import json as _json

        from tree_options.trex.discovery.artifact import read_result

        state = tmp_path / "state"
        write_scan_request(state / "spool", "req-down", NOW)
        body = _json.dumps(
            {"timestamp": "t", "data": {"bid": 1.0, "ask": 1.1, "close": 1.0}}
        ).encode()
        ran = serve_tick(
            FakeChainSource(),
            _cfg(),
            state,
            now=NOW,
            market_transport=self._transport(body),
            broker_ready=False,
        )
        assert ran is False
        result = read_result(state / "spool", "scan")
        assert result is not None and result["status"] == "error"
        assert "gateway" in result["detail"]
        # market work never needed the broker
        assert (state / "market.json").exists()
        # and no scan run directory materialized
        runs = state / "runs"
        assert not runs.exists() or not list(runs.glob("*"))

    def test_broker_ready_lazy_connect(self, tmp_path: Path) -> None:
        from tree_options.trex.discovery.runner import _broker_ready

        class DeadGateway(FakeChainSource):
            def connected(self) -> bool:
                return False

            def connect(self) -> None:
                raise TimeoutError("gateway down")

        class Revives(FakeChainSource):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def connected(self) -> bool:
                return self.calls > 0

            def connect(self) -> None:
                self.calls += 1

        assert _broker_ready(FakeChainSource()) is True  # no probe attr = ready
        assert _broker_ready(DeadGateway()) is False
        assert _broker_ready(Revives()) is True

    def test_backoff_spaces_dead_gateway_retries(self) -> None:
        from tree_options.trex.discovery.runner import ConnectBackoff, _broker_ready

        attempts: list[int] = []

        class DeadGateway(FakeChainSource):
            def connected(self) -> bool:
                return False

            def connect(self) -> None:
                attempts.append(1)
                raise TimeoutError("gateway down")

        clock = [1000.0]
        backoff = ConnectBackoff(seconds=120, clock=lambda: clock[0])
        gw = DeadGateway()
        assert _broker_ready(gw, backoff) is False
        clock[0] += 5  # next serve tick: inside the window, no connect
        assert _broker_ready(gw, backoff) is False
        assert len(attempts) == 1
        clock[0] += 120  # window passed: one more attempt
        assert _broker_ready(gw, backoff) is False
        assert len(attempts) == 2


class TestBacktestTick:
    """M4: the runner materializes spooled valuation scenarios from its
    OWN artifacts (cache-only when no transport is wired)."""

    KEY = "QQQ|20261016|642|657"

    def _seed(self, state: Path, *, quote: bool = True, bars: bool = True) -> None:
        import json as _json

        state.mkdir(parents=True, exist_ok=True)
        (state / "latest.json").write_text(_json.dumps({"payload": {
            "candidates": [{"underlying": "QQQ", "expiry": "20261016", "dte": 24,
                            "short_strike": 642.0, "long_strike": 657.0,
                            "debit_mid": 0.195, "debit_ask": 0.23}],
            "rejected": [],
        }}))
        if quote:
            (state / "market.json").write_text(_json.dumps({
                "last_refresh": NOW.isoformat(),
                "symbols": {"QQQ": {"bid": 747.97, "ask": 748.0, "iv30": 17.5}},
                "errors": {},
            }))
        if bars:
            base = 1_760_000_000_000
            rows = [{"t": base + i * 86_400_000, "c": 700.0 + i * 0.2} for i in range(120)]
            cache = state / "market" / "cache" / "bars"
            cache.mkdir(parents=True, exist_ok=True)
            (cache / "QQQ.json").write_text(_json.dumps(
                {"fetched_at": NOW.isoformat(), "ttl_seconds": 86400,
                 "payload": {"bars": rows}}
            ))

    def test_materializes_labeled_artifact(self, tmp_path: Path) -> None:
        from tree_options.trex.discovery.artifact import read_result, write_request
        from tree_options.trex.discovery.backtest import LABEL, read_artifact

        state = tmp_path / "state"
        self._seed(state)
        write_request(state / "spool", "backtest", "bt-1",
                      {"request_ts": NOW.isoformat(), "key": self.KEY})
        serve_tick(FakeChainSource(), _cfg(), state, now=NOW, broker_ready=False)
        doc = read_artifact(state, self.KEY)
        assert doc is not None and doc["error"] is None
        assert doc["label"] == LABEL
        assert doc["structure"]["dte"] == 24  # recomputed from expiry vs now
        assert doc["assumptions"]["exits_modeled"].startswith("none")
        assert doc["analogs"]["count"] > 50
        result = read_result(state / "spool", "backtest")
        assert result is not None and result["status"] == "ok"
        assert not list((state / "spool").glob("backtest.request.*"))

    def test_missing_quote_completes_with_error_artifact(self, tmp_path: Path) -> None:
        from tree_options.trex.discovery.artifact import read_result, write_request
        from tree_options.trex.discovery.backtest import read_artifact

        state = tmp_path / "state"
        self._seed(state, quote=False)
        write_request(state / "spool", "backtest", "bt-2",
                      {"request_ts": NOW.isoformat(), "key": self.KEY})
        serve_tick(FakeChainSource(), _cfg(), state, now=NOW)
        doc = read_artifact(state, self.KEY)
        assert doc is not None and "quote" in doc["error"]
        result = read_result(state / "spool", "backtest")
        assert result is not None and result["status"] == "error"

    def test_unknown_key_errors_not_spins(self, tmp_path: Path) -> None:
        from tree_options.trex.discovery.artifact import write_request
        from tree_options.trex.discovery.backtest import read_artifact

        state = tmp_path / "state"
        self._seed(state)
        write_request(state / "spool", "backtest", "bt-3",
                      {"request_ts": NOW.isoformat(), "key": "SPY|20261016|500|510"})
        serve_tick(FakeChainSource(), _cfg(), state, now=NOW)
        doc = read_artifact(state, "SPY|20261016|500|510")
        assert doc is not None and "not found" in doc["error"]

    def test_calendar_dte(self) -> None:
        from tree_options.trex.discovery.runner import _calendar_dte

        assert _calendar_dte("20261016", NOW) == 24
        assert _calendar_dte("20260901", NOW) == 0  # past expiry floors at 0


class TestProposals:
    """M6: proposals are broker-independent, verified against a real CBOE
    quote, recorded PENDING, and can never fail a scan."""

    def _llm(self, content: str, calls: list | None = None):
        def t(url: str, body: bytes, headers: dict, timeout: float):
            if calls is not None:
                calls.append(url)
            return 200, json.dumps({"choices": [{"message": {"content": content}}]}).encode()

        return t

    def _market(self):
        def t(url: str, *, timeout: float = 10.0):
            if "FAKEX" in url:
                return 404, b""
            return 200, json.dumps({"timestamp": "2026-09-22 20:00:00",
                                    "data": {"bid": 1.0, "ask": 1.1, "close": 1.0}}).encode()

        return t

    def test_on_demand_records_verified_pending(self, tmp_path: Path) -> None:
        from tree_options.trex.discovery.artifact import read_result, write_request
        from tree_options.trex.discovery.watchlist import load_watchlist

        state = tmp_path / "state"
        load_watchlist(state, seed=["NVDA", "QQQ"], now=NOW)
        write_request(state / "spool", "propose", "p-1", {"request_ts": NOW.isoformat()})
        reply = json.dumps({"proposals": [
            {"symbol": "TSM", "action": "add", "rationale": "semis breadth", "confidence": 0.6},
            {"symbol": "FAKEX", "action": "add", "rationale": "hallucinated"},
        ]})
        serve_tick(FakeChainSource(), _cfg(), state, now=NOW, broker_ready=False,
                   market_transport=self._market(), llm_transport=self._llm(reply))
        doc = load_watchlist(state)
        pending = [p for p in doc["proposals"] if p["status"] == "pending"]
        assert [p["symbol"] for p in pending] == ["TSM"]
        assert pending[0]["provenance"]["provider"] == "local"
        run = doc["last_proposal_run"]
        assert run["status"] == "ok" and any("FAKEX" in n for n in run["notes"])
        result = read_result(state / "spool", "propose")
        assert result is not None and result["status"] == "ok" and result["count"] == 2

    def test_llm_failure_never_fails_the_scan(self, tmp_path: Path) -> None:
        def boom(url: str, body: bytes, headers: dict, timeout: float):
            raise OSError("llm lane down")

        state = tmp_path / "state"
        write_scan_request(state / "spool", "req-llm", NOW)
        ran = serve_tick(FakeChainSource(), _cfg(), state, now=NOW, llm_transport=boom)
        assert ran is True
        result = read_scan_result(state / "spool")
        assert result is not None and result["status"] == "ok"
        from tree_options.trex.discovery.watchlist import load_watchlist

        assert load_watchlist(state)["last_proposal_run"]["status"] == "failed"

    def test_post_scan_hook_is_rate_limited(self, tmp_path: Path) -> None:
        calls: list[str] = []
        state = tmp_path / "state"
        llm = self._llm('{"proposals": []}', calls)
        write_scan_request(state / "spool", "req-a", NOW)
        serve_tick(FakeChainSource(), _cfg(), state, now=NOW, llm_transport=llm)
        later = datetime(2026, 9, 22, 18, 0, tzinfo=ET)  # < 6h later
        write_scan_request(state / "spool", "req-b", later)
        serve_tick(FakeChainSource(), _cfg(), state, now=later, llm_transport=llm)
        assert len(calls) == 1

    def test_no_llm_transport_means_no_llm_work(self, tmp_path: Path) -> None:
        from tree_options.trex.discovery.artifact import write_request

        state = tmp_path / "state"
        write_request(state / "spool", "propose", "p-2", {"request_ts": NOW.isoformat()})
        serve_tick(FakeChainSource(), _cfg(), state, now=NOW)
        assert list((state / "spool").glob("propose.request.*"))  # left for a real runner
