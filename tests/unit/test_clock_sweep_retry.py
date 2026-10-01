"""The clock-tier sweep retry + coverage watch.

Oracle discipline: coverage counts and sleep totals come from hand-built
transport schedules, never from the implementation's own accounting.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from tests.fixtures.desk_cboe import chain_payload, encode
from tree_options.desk import store
from tree_options.trex.clock import ET

D = date(2026, 10, 1)


@pytest.fixture(autouse=True)
def _pinned_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DESK_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("TREX_DESK_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("DESK_PAPER_DIR", str(tmp_path / "paper"))


def _body(sym: str, ts_utc: str) -> bytes:
    return encode(
        chain_payload(sym, timestamp=ts_utc, underlying_last="2026-10-01T10:02:30")
    )


class RollingTransport:
    """Serves fresh payloads except the names in `late`, which roll to fresh
    content only after `rolls_at` reads of them (the late-roll cohort)."""

    def __init__(self, symbols: list[str], late: set[str], rolls_at: int) -> None:
        self.reads: dict[str, int] = {s: 0 for s in symbols}
        self.symbols, self.late, self.rolls_at = symbols, late, rolls_at

    def __call__(self, url: str, *, timeout: float = 30.0) -> tuple[int, bytes]:
        sym = url.rsplit("/", 1)[1].removesuffix(".json")
        if sym not in self.reads:
            return 404, b""
        self.reads[sym] += 1
        fresh = "2026-10-01 14:02:00"  # 10:02 ET: inside the 10:00 window
        stale = "2026-10-01 13:58:00"  # 9:58 ET: predates the clock
        if sym in self.late and self.reads[sym] <= self.rolls_at:
            return 200, _body(sym, stale)
        return 200, _body(sym, fresh)


def _record(tmp_path: Path, static_calendar, transport, *, clock="10:00"):
    sleeps: list[float] = []
    return (
        store.record_session(
            D,
            list(transport.reads),
            store=store.ChainStore(tmp_path / "store"),
            transport=transport,
            clock=lambda: datetime(2026, 10, 1, 10, 4, tzinfo=ET),
            sleep=sleeps.append,
            cal=static_calendar,
            tier=clock,
        ),
        sleeps,
        tmp_path / "store",
    )


class TestSweepRetry:
    def test_late_rollers_recover_on_the_retry_pass(self, tmp_path, static_calendar) -> None:
        names = ["KO", "PEP", "PG"]
        # PG is stale on the first sweep, rolls by its 2nd read (the retry)
        t = RollingTransport(names, late={"PG"}, rolls_at=1)
        summary, sleeps, root = _record(tmp_path, static_calendar, t)
        assert {s: r.status for s, r in summary.results.items()} == {
            "KO": "ok",
            "PEP": "ok",
            "PG": "ok",
        }
        assert (root / "chains/clock=10:00/2026-10-01/PG.json.gz").exists()

    def test_never_rollers_stay_stale_and_passes_are_bounded(
        self, tmp_path, static_calendar
    ) -> None:
        names = ["KO", "PG"]
        t = RollingTransport(names, late={"PG"}, rolls_at=99)  # never rolls
        summary, sleeps, root = _record(tmp_path, static_calendar, t)
        assert summary.results["KO"].status == "ok"
        assert summary.results["PG"].status == "stale"
        # oracle: the pause fires exactly once per retry pass (2 passes, PG
        # stale through both), and PG is fetched 1 (sweep) + 2 (retries) = 3x
        assert sleeps.count(store.CLOCK_RETRY_PAUSE_S) == store.CLOCK_RETRY_PASSES
        assert t.reads["PG"] == 1 + store.CLOCK_RETRY_PASSES

    def test_eod_tier_never_retries(self, tmp_path, static_calendar) -> None:
        # eod semantics untouched: a stale overnight payload stays stale,
        # no pause sleeps, exactly one fetch (the A2 retry is clock-only)
        bodies = {"KO": encode(chain_payload(session="2026-09-30", timestamp="2026-10-01 03:40"))}
        calls = []

        def t(url: str, *, timeout: float = 30.0) -> tuple[int, bytes]:
            calls.append(url)
            return 200, bodies["KO"]

        sleeps: list[float] = []
        summary = store.record_session(
            D,
            ["KO"],
            store=store.ChainStore(tmp_path / "store"),
            transport=t,
            clock=lambda: datetime(2026, 10, 2, 6, 30, tzinfo=ET),
            sleep=sleeps.append,
            cal=static_calendar,
        )
        assert summary.results["KO"].status != "ok"
        assert len(calls) == 1
        assert sleeps == [store.PACE_S] * 0  # single symbol: no pacing either


class TestClockCoverage:
    def _manifests(self, root: Path) -> None:
        for clock, rows in {
            "10:00": {"KO": "ok", "PEP": "ok", "PG": "stale"},
            "10:45": {"KO": "ok", "PEP": "stale", "PG": "stale"},
        }.items():
            path = root / "manifest" / f"clock={clock}" / "2026-10-01.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "schema": "desk-chain-manifest/1",
                        "session": "2026-10-01",
                        "clock": clock,
                        "symbols": {s: {"status": st, "n": 1, "raw_sha256": "x", "detail": "", "at": "t"} for s, st in rows.items()},
                    }
                )
            )

    def test_rows_and_the_persistent_stale_set(self, tmp_path: Path) -> None:
        self._manifests(tmp_path / "store")
        rows = store.ChainStore(tmp_path / "store").clock_coverage(D)
        assert [(r["clock"], r["ok"], r["stale"]) for r in rows] == [
            ("10:00", 2, 1),
            ("10:45", 1, 2),
        ]
        # PG is stale in BOTH; PEP only in the later clock
        assert rows[0]["stale_names"] == ["PG"]
        assert rows[1]["stale_names"] == ["PEP", "PG"]

    def test_cli_prints_and_alerts_below_min_ok(
        self, tmp_path: Path, monkeypatch, capsys, static_calendar
    ) -> None:
        from tree_options.desk.__main__ import run_cli

        self._manifests(tmp_path / "store")
        rc = run_cli(
            ["clock-coverage", "--session", "2026-10-01", "--min-ok", "1"],
            transport=None,
            sleep=lambda _s: None,
            now=datetime(2026, 10, 1, 12, 0, tzinfo=ET),
            cal=static_calendar,
        )
        out = capsys.readouterr()
        assert rc == 0
        assert "clock 10:00: ok 2 stale 1" in out.out
        assert "persistent stale (every clock): PG" in out.out
        rc2 = run_cli(
            ["clock-coverage", "--session", "2026-10-01", "--min-ok", "3"],
            transport=None,
            sleep=lambda _s: None,
            now=datetime(2026, 10, 1, 12, 0, tzinfo=ET),
            cal=static_calendar,
        )
        err = capsys.readouterr().err
        assert rc2 == 1
        assert "ALERT" in err and "10:45=1" in err

    def test_no_manifests_exit_3(self, tmp_path: Path, static_calendar, capsys) -> None:
        from tree_options.desk.__main__ import run_cli

        rc = run_cli(
            ["clock-coverage", "--session", "2026-10-01"],
            transport=None,
            sleep=lambda _s: None,
            now=datetime(2026, 10, 1, 12, 0, tzinfo=ET),
            cal=static_calendar,
        )
        assert rc == 3
        assert "no clock-tier manifests" in capsys.readouterr().out
