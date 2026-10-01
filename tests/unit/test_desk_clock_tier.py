"""A2 clock tier: the intraday CBOE store dimension (design in
docs/desk/CAMPAIGN-EXIT-20260930.md, revived by the 2026-10-01 A0 probe).

Oracle note: these tests build payloads through the shared CBOE fixture
(timestamps in UTC, per the pinned HTTP Last-Modified evidence) and assert
on OBSERVED filesystem layout and verdict statuses - never on the
implementation's own constants.
"""

from __future__ import annotations

import gzip
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from tests.fixtures.desk_cboe import chain_payload, encode
from tree_options.desk import chains, store
from tree_options.trex.clock import ET

D = date(2026, 9, 22)  # Tuesday


@pytest.fixture(autouse=True)
def _pinned_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DESK_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("TREX_DESK_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("DESK_PAPER_DIR", str(tmp_path / "paper"))


def _clock_payload(sym: str = "KO", *, ts_utc: str) -> bytes:
    """A payload whose publication instant lands at 10:02 ET unless told."""
    return encode(
        chain_payload(
            sym,
            timestamp=ts_utc,
            underlying_last="2026-09-22T10:02:30",
        )
    )


class TestClockPaths:
    def test_eod_path_unchanged_and_clock_namespace_separate(self, tmp_path: Path) -> None:
        s = store.ChainStore(tmp_path)
        assert s.chain_path(D, "KO") == tmp_path / "chains/2026-09-22/KO.json.gz"
        assert s.chain_path(D, "KO", clock="eod") == tmp_path / "chains/2026-09-22/KO.json.gz"
        assert (
            s.chain_path(D, "KO", clock="10:00")
            == tmp_path / "chains/clock=10:00/2026-09-22/KO.json.gz"
        )

    def test_clock_manifest_is_per_tier_and_eod_manifest_untouched(self, tmp_path: Path) -> None:
        s = store.ChainStore(tmp_path)
        assert s.manifest_path(D) == tmp_path / "manifest/2026-09-22.json"
        assert (
            s.manifest_path(D, clock="15:15")
            == tmp_path / "manifest/clock=15:15/2026-09-22.json"
        )
        # the clock manifests are DIRECTORIES under manifest/: the eod
        # session enumeration globs *.json and cannot see them
        (tmp_path / "manifest").mkdir(parents=True)
        (tmp_path / "manifest/2026-09-22.json").write_text("{}")
        (tmp_path / "manifest/clock=10:00").mkdir(parents=True)
        (tmp_path / "manifest/clock=10:00/2026-09-22.json").write_text("{}")
        assert s.manifest_sessions() == [D]


class TestClockValidate:
    def _v(self, ts_utc: str, clock: str = "10:00"):
        parsed = chains.parse_chain(_clock_payload(ts_utc=ts_utc), "KO")
        return store.validate(parsed, D, None, clock=clock)

    def test_fresh_publication_inside_the_clock_window_is_ok(self) -> None:
        assert self._v("2026-09-22 14:02:00").status == "ok"

    def test_pre_clock_publication_is_stale_not_the_clocks_observation(self) -> None:
        v = self._v("2026-09-22 13:58:00")
        assert v.status == "stale"
        assert "predates the 10:00 clock" in v.detail

    def test_publication_past_the_window_is_stale(self) -> None:
        v = self._v("2026-09-22 14:20:00")
        assert v.status == "stale"
        assert "past the 10:00 clock window" in v.detail

    def test_clock_instant_is_et_not_utc(self) -> None:
        # 15:15 ET = 19:15 UTC: a 19:16 UTC stamp is the 15:15 clock's content
        parsed = chains.parse_chain(
            _clock_payload(ts_utc="2026-09-22 19:16:00"), "KO"
        )
        assert store.validate(parsed, D, None, clock="15:15").status == "ok"

    def test_eod_semantics_unchanged_without_clock(self, static_calendar) -> None:
        # the same payload that is fresh for a clock FAILS eod validation
        # (no close-settle evidence): the tiers never borrow each other's ok
        parsed = chains.parse_chain(_clock_payload(ts_utc="2026-09-22 14:02:00"), "KO")
        v = store.validate(parsed, D, static_calendar)
        assert v.status != "ok"


class TestClockRecording:
    def _record(self, tmp_path, static_calendar, *, clock: str, ts: str = "14:02:00"):
        bodies = {
            sym: _clock_payload(sym, ts_utc=f"2026-09-22 {ts}")
            for sym in ("KO", "PEP")
        }
        return (
            store.record_session(
                D,
                sorted(bodies),
                store=store.ChainStore(tmp_path / "store"),
                transport=_transport(bodies),
                clock=lambda: datetime(2026, 9, 22, 10, 4, tzinfo=ET),
                sleep=lambda _s: None,
                cal=static_calendar,
                tier=clock,
            ),
            tmp_path / "store",
        )

    def test_clock_session_writes_its_own_namespace_and_no_raw(self, tmp_path, static_calendar) -> None:
        summary, root = self._record(tmp_path, static_calendar, clock="10:00")
        assert all(r.status == "ok" for r in summary.results.values())
        assert (root / "chains/clock=10:00/2026-09-22/KO.json.gz").exists()
        assert (root / "chains/clock=10:00/2026-09-22/PEP.json.gz").exists()
        assert not (root / "chains/2026-09-22/KO.json.gz").exists()  # eod untouched
        assert not (root / "raw").exists()  # the clock tier keeps no raw evidence

    def test_clock_documents_carry_the_clock_header(self, tmp_path, static_calendar) -> None:
        self._record(tmp_path, static_calendar, clock="10:00")
        with gzip.open(tmp_path / "store/chains/clock=10:00/2026-09-22/KO.json.gz") as fh:
            doc = json.loads(fh.read())
        assert doc["header"]["clock"] == "10:00"
        assert doc["header"]["schema"] == "desk-chain/1"  # no version fork

    def test_eod_documents_now_carry_the_eod_clock_field(self, tmp_path, static_calendar) -> None:
        bodies = {"KO": encode(chain_payload())}
        store.record_session(
            D,
            ["KO"],
            store=store.ChainStore(tmp_path / "store"),
            transport=_transport(bodies),
            clock=lambda: datetime(2026, 9, 23, 6, 30, tzinfo=ET),
            sleep=lambda _s: None,
            cal=static_calendar,
        )
        with gzip.open(tmp_path / "store/chains/2026-09-22/KO.json.gz") as fh:
            doc = json.loads(fh.read())
        assert doc["header"]["clock"] == "eod"

    def test_per_clock_manifest_and_no_eod_bookkeeping(self, tmp_path, static_calendar) -> None:
        self._record(tmp_path, static_calendar, clock="10:00")
        m = json.loads((tmp_path / "store/manifest/clock=10:00/2026-09-22.json").read_text())
        assert m["clock"] == "10:00"
        assert set(m["symbols"]) == {"KO", "PEP"}
        assert not (tmp_path / "store/manifest/2026-09-22.json").exists()
        assert not (tmp_path / "store/gaps.jsonl").exists()  # not the clock tier's lane


def _transport(bodies: dict[str, bytes]):
    def t(url: str, *, timeout: float = 30.0) -> tuple[int, bytes]:
        sym = url.rsplit("/", 1)[1].removesuffix(".json")
        body = bodies.get(sym)
        return (404, b"") if body is None else (200, body)

    return t


class TestClockCli:
    def _run(self, monkeypatch, tmp_path, *, argv, bodies, now, static_calendar):
        from tree_options.desk.__main__ import run_cli

        return run_cli(
            argv,
            transport=_transport(bodies),
            sleep=lambda _s: None,
            now=now,
            cal=static_calendar,
        )

    def test_auto_records_the_open_clock(self, monkeypatch, tmp_path: Path, static_calendar) -> None:
        bodies = {
            sym: _clock_payload(sym, ts_utc="2026-09-22 14:02:00")
            for sym in ("KO", "PEP", "SPY")  # SPY: the probe reads it first
        }
        rc = self._run(
            monkeypatch,
            tmp_path,
            static_calendar=static_calendar,
            argv=["record-chains", "--clock", "auto", "--symbols", "KO,PEP"],
            bodies=bodies,
            now=datetime(2026, 9, 22, 10, 4, tzinfo=ET),
        )
        assert rc == 0
        assert (tmp_path / "store/chains/clock=10:00/2026-09-22/KO.json.gz").exists()

    def test_auto_outside_the_schedule_exits_zero(self, monkeypatch, tmp_path, capsys, static_calendar) -> None:
        rc = self._run(
            monkeypatch,
            tmp_path,
            static_calendar=static_calendar,
            argv=["record-chains", "--clock", "auto", "--symbols", "KO"],
            bodies={},
            now=datetime(2026, 9, 22, 9, 59, tzinfo=ET),  # before the first clock
        )
        assert rc == 0
        assert "no decision clock is open" in capsys.readouterr().err

    def test_non_session_day_refused(self, monkeypatch, tmp_path, static_calendar) -> None:
        rc = self._run(
            monkeypatch,
            tmp_path,
            static_calendar=static_calendar,
            argv=["record-chains", "--clock", "auto"],
            bodies={},
            now=datetime(2026, 9, 26, 10, 4, tzinfo=ET),  # Saturday
        )
        assert rc == 2

    def test_clock_not_in_schedule_refused(self, monkeypatch, tmp_path, static_calendar) -> None:
        rc = self._run(
            monkeypatch,
            tmp_path,
            static_calendar=static_calendar,
            argv=["record-chains", "--clock", "09:31"],
            bodies={},
            now=datetime(2026, 9, 22, 10, 4, tzinfo=ET),
        )
        assert rc == 2

    def test_probe_retries_until_the_publication_rolls(
        self, monkeypatch, tmp_path: Path, static_calendar
    ) -> None:
        # first SPY read still carries pre-clock content; later reads rolled
        spy_feed = [
            encode(chain_payload("SPY", timestamp="2026-09-22 13:58:00")),
            encode(chain_payload("SPY", timestamp="2026-09-22 14:01:00")),
        ]
        bodies = {
            "SPY": b"",  # sentinel; the transport below overrides SPY
            "KO": _clock_payload("KO", ts_utc="2026-09-22 14:02:00"),
        }
        spy_reads = [0]

        def t(url: str, *, timeout: float = 30.0) -> tuple[int, bytes]:
            sym = url.rsplit("/", 1)[1].removesuffix(".json")
            if sym == "SPY":
                # the probe may consume early reads; the sweep sticks on the last
                i = min(spy_reads[0], len(spy_feed) - 1)
                spy_reads[0] += 1
                return 200, spy_feed[i]
            return 200, bodies[sym]

        from tree_options.desk.__main__ import run_cli

        rc = run_cli(
            ["record-chains", "--clock", "auto", "--symbols", "KO,SPY"],
            transport=t,
            sleep=lambda _s: None,
            now=datetime(2026, 9, 22, 10, 3, tzinfo=ET),
            cal=static_calendar,
        )
        assert rc == 0
        assert (tmp_path / "store/chains/clock=10:00/2026-09-22/SPY.json.gz").exists()
