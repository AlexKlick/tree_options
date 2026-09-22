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
