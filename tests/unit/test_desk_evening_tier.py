"""The evening observation tier: one post-close chain observation per
session per name (chains/clock=evening/<D>/), built for the day-1
persistent late cohort (2026-10-01: CRM/DIS/LLY/PEP/PG/V/XLE/XLV stale at
EVERY clock; their delayed publications never roll inside a 10-minute
window but have fully rolled by evening). Design:
docs/desk/EVENING-TIER.md.

Oracle note: payloads come from the shared CBOE fixture (UTC timestamps,
per the pinned HTTP Last-Modified evidence) and assertions are on
OBSERVED filesystem layout, verdict statuses and transport call counts —
never on the implementation's own accounting.
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

D = date(2026, 10, 1)  # Thursday, session day
CLOSE_UTC = "2026-10-01 20:00:00"  # 16:00 ET = the session close (EDT)


@pytest.fixture(autouse=True)
def _pinned_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DESK_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("TREX_DESK_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("DESK_PAPER_DIR", str(tmp_path / "paper"))


def _evening_payload(
    sym: str = "KO",
    *,
    ts_utc: str = "2026-10-01 21:30:00",  # 17:30 ET: post-close, same evening
    underlying_last: str = "2026-10-01T15:59:59",
) -> bytes:
    """A payload whose publication instant is 17:30 ET on D unless told."""
    return encode(
        chain_payload(
            sym,
            session="2026-10-01",
            timestamp=ts_utc,
            underlying_last=underlying_last,
        )
    )


def _transport(bodies: dict[str, bytes]):
    def t(url: str, *, timeout: float = 30.0) -> tuple[int, bytes]:
        sym = url.rsplit("/", 1)[1].removesuffix(".json")
        body = bodies.get(sym)
        return (404, b"") if body is None else (200, body)

    return t


class TestEveningValidate:
    def _v(self, ts_utc: str, static_calendar, *, underlying_last: str = "2026-10-01T15:59:59"):
        parsed = chains.parse_chain(
            _evening_payload(ts_utc=ts_utc, underlying_last=underlying_last), "KO"
        )
        return store.validate(parsed, D, static_calendar, clock=store.EVENING)

    def test_post_close_publication_same_evening_is_ok(self, static_calendar) -> None:
        assert self._v("2026-10-01 21:30:00", static_calendar).status == "ok"  # 17:30 ET

    def test_publication_at_exactly_the_session_close_is_ok(self, static_calendar) -> None:
        assert self._v(CLOSE_UTC, static_calendar).status == "ok"  # 16:00 ET: at-or-after

    def test_pre_close_publication_is_stale_by_design(self, static_calendar) -> None:
        v = self._v("2026-10-01 19:30:00", static_calendar)  # 15:30 ET: never rolled
        assert v.status == "stale"
        assert "16:00 ET session close" in v.detail

    def test_early_close_session_uses_the_calendars_close(self, static_calendar) -> None:
        # 2026-11-27 is the (1 pm ET) early close in the pinned calendar:
        # a 13:05 ET stamp is fresh for that session's evening tier
        parsed = chains.parse_chain(
            encode(
                chain_payload(
                    "KO",
                    session="2026-11-27",
                    timestamp="2026-11-27 18:05:00",  # 13:05 ET
                    underlying_last="2026-11-27T12:59:59",
                )
            ),
            "KO",
        )
        v = store.validate(parsed, date(2026, 11, 27), static_calendar, clock=store.EVENING)
        assert v.status == "ok"

    def test_frozen_intraday_content_is_stale_evening_or_not(self, static_calendar) -> None:
        # stamped 18:10 ET but the underlying last traded 10:02 (the eod
        # tier's settle-evidence check, mirrored: a never-rolled snapshot)
        v = self._v("2026-10-01 22:10:00", static_calendar, underlying_last="2026-10-01T10:02:30")
        assert v.status == "stale"
        assert "predates the settle" in v.detail

    def test_tiers_never_borrow_each_others_freshness(self, static_calendar) -> None:
        # the same payload is fresh for evening but PAST the 15:15 clock
        # window, and a clock-fresh 10:02 ET payload is before the close
        evening_ok = chains.parse_chain(_evening_payload(ts_utc="2026-10-01 21:30:00"), "KO")
        assert (
            store.validate(evening_ok, D, static_calendar, clock="15:15").status == "stale"
        )
        clock_ok = chains.parse_chain(
            _evening_payload(ts_utc="2026-10-01 14:02:00"), "KO"
        )
        assert store.validate(clock_ok, D, static_calendar, clock=store.EVENING).status == "stale"


class TestEveningRecording:
    def _record(
        self, tmp_path: Path, static_calendar, *, bodies: dict[str, bytes], tier=store.EVENING
    ):
        sleeps: list[float] = []
        return (
            store.record_session(
                D,
                sorted(bodies),
                store=store.ChainStore(tmp_path / "store"),
                transport=_transport(bodies),
                clock=lambda: datetime(2026, 10, 1, 18, 10, tzinfo=ET),
                sleep=sleeps.append,
                cal=static_calendar,
                tier=tier,
            ),
            sleeps,
            tmp_path / "store",
        )

    def test_evening_writes_its_own_namespace_and_no_raw(
        self, tmp_path, static_calendar
    ) -> None:
        bodies = {s: _evening_payload(s) for s in ("KO", "PEP")}
        summary, _, root = self._record(tmp_path, static_calendar, bodies=bodies)
        assert all(r.status == "ok" for r in summary.results.values())
        assert (root / "chains/clock=evening/2026-10-01/KO.json.gz").exists()
        assert (root / "chains/clock=evening/2026-10-01/PEP.json.gz").exists()
        assert not (root / "chains/2026-10-01/KO.json.gz").exists()  # eod untouched
        assert not (root / "chains/clock=10:00").exists()  # no clock namespace either
        assert not (root / "raw").exists()  # evening keeps no raw evidence

    def test_evening_documents_carry_the_evening_clock_header(
        self, tmp_path, static_calendar
    ) -> None:
        self._record(
            tmp_path, static_calendar, bodies={"KO": _evening_payload("KO")}
        )
        with gzip.open(tmp_path / "store/chains/clock=evening/2026-10-01/KO.json.gz") as fh:
            doc = json.loads(fh.read())
        assert doc["header"]["clock"] == "evening"
        assert doc["header"]["schema"] == "desk-chain/1"  # no version fork

    def test_evening_manifest_is_per_tier_and_eod_bookkeeping_untouched(
        self, tmp_path, static_calendar
    ) -> None:
        self._record(
            tmp_path, static_calendar, bodies={"KO": _evening_payload("KO")}
        )
        m = json.loads(
            (tmp_path / "store/manifest/clock=evening/2026-10-01.json").read_text()
        )
        assert m["clock"] == "evening"
        assert set(m["symbols"]) == {"KO"}
        assert not (tmp_path / "store/manifest/2026-10-01.json").exists()
        assert not (tmp_path / "store/gaps.jsonl").exists()  # not the eod lane

    def test_evening_eod_and_clock_namespaces_coexist(
        self, tmp_path, static_calendar
    ) -> None:
        # the same evening bytes also satisfy eod (post-close, settled), and
        # a clock payload lands in its own namespace: three namespaces, one
        # file each, nothing shared
        def run(tier: str, body: bytes) -> None:
            store.record_session(
                D,
                ["KO"],
                store=store.ChainStore(tmp_path / "store"),
                transport=_transport({"KO": body}),
                clock=lambda: datetime(2026, 10, 1, 18, 10, tzinfo=ET),
                sleep=lambda _s: None,
                cal=static_calendar,
                tier=tier,
            )

        run(store.EOD, _evening_payload("KO"))
        run("10:00", _evening_payload("KO", ts_utc="2026-10-01 14:02:00"))
        run(store.EVENING, _evening_payload("KO"))
        assert (tmp_path / "store/chains/2026-10-01/KO.json.gz").exists()
        assert (tmp_path / "store/raw/2026-10-01").is_dir()  # eod keeps raw
        assert (tmp_path / "store/chains/clock=10:00/2026-10-01/KO.json.gz").exists()
        assert (tmp_path / "store/chains/clock=evening/2026-10-01/KO.json.gz").exists()
        assert len(list((tmp_path / "store/chains/2026-10-01").glob("*.json.gz"))) == 1
        assert len(list((tmp_path / "store/chains/clock=evening/2026-10-01").glob("*.json.gz"))) == 1


class TestEveningSinglePass:
    def test_one_fetch_per_symbol_no_retry_passes(self, tmp_path, static_calendar) -> None:
        # PG's publication never rolled past the close: it stays stale and
        # is fetched EXACTLY once (the #61 retry passes are clock-tiers-only)
        reads: dict[str, int] = {s: 0 for s in ("KO", "PEP", "PG")}

        def t(url: str, *, timeout: float = 30.0) -> tuple[int, bytes]:
            sym = url.rsplit("/", 1)[1].removesuffix(".json")
            reads[sym] += 1
            ts = "2026-10-01 19:30:00" if sym == "PG" else "2026-10-01 21:30:00"
            return 200, _evening_payload(sym, ts_utc=ts)

        sleeps: list[float] = []
        summary = store.record_session(
            D,
            sorted(reads),
            store=store.ChainStore(tmp_path / "store"),
            transport=t,
            clock=lambda: datetime(2026, 10, 1, 18, 10, tzinfo=ET),
            sleep=sleeps.append,
            cal=static_calendar,
            tier=store.EVENING,
        )
        assert {s: r.status for s, r in summary.results.items()} == {
            "KO": "ok",
            "PEP": "ok",
            "PG": "stale",
        }
        assert reads == {"KO": 1, "PEP": 1, "PG": 1}  # one fetch per symbol
        assert store.CLOCK_RETRY_PAUSE_S not in sleeps  # no retry pauses
        assert not (tmp_path / "store/chains/clock=evening/2026-10-01/PG.json.gz").exists()


class TestEveningCoverage:
    def test_evening_row_appears_in_clock_coverage_with_the_clock_rows(
        self, tmp_path, static_calendar
    ) -> None:
        root = tmp_path / "store"
        path = root / "manifest/clock=10:00/2026-10-01.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "schema": "desk-chain-manifest/1",
                    "session": "2026-10-01",
                    "clock": "10:00",
                    "symbols": {
                        s: {"status": st, "n": 1, "raw_sha256": "x", "detail": "", "at": "t"}
                        for s, st in {"KO": "ok", "PG": "stale"}.items()
                    },
                }
            )
        )
        bodies = {
            s: _evening_payload(s, ts_utc="2026-10-01 19:30:00" if s == "PG" else "2026-10-01 21:30:00")
            for s in ("KO", "PEP", "PG")
        }
        store.record_session(
            D,
            sorted(bodies),
            store=store.ChainStore(root),
            transport=_transport(bodies),
            clock=lambda: datetime(2026, 10, 1, 18, 10, tzinfo=ET),
            sleep=lambda _s: None,
            cal=static_calendar,
            tier=store.EVENING,
        )
        rows = store.ChainStore(root).clock_coverage(D)
        # the evening manifest is the store's own write, surfaced next to
        # the clock rows (the coverage glob is clock=*): evening sorts last
        assert [(r["clock"], r["ok"], r["stale"]) for r in rows] == [
            ("10:00", 1, 1),
            ("evening", 2, 1),
        ]
        assert rows[1]["stale_names"] == ["PG"]


class TestEveningCli:
    def _run(self, tmp_path, static_calendar, *, argv, bodies, now):
        from tree_options.desk.__main__ import run_cli

        return run_cli(
            argv,
            transport=_transport(bodies),
            sleep=lambda _s: None,
            now=now,
            cal=static_calendar,
        )

    def test_clock_evening_records_the_tier(
        self, tmp_path: Path, static_calendar
    ) -> None:
        rc = self._run(
            tmp_path,
            static_calendar,
            argv=["record-chains", "--clock", "evening", "--symbols", "KO,PEP"],
            bodies={s: _evening_payload(s) for s in ("KO", "PEP")},
            now=datetime(2026, 10, 1, 18, 10, tzinfo=ET),
        )
        assert rc == 0
        assert (tmp_path / "store/chains/clock=evening/2026-10-01/KO.json.gz").exists()
        assert not (tmp_path / "store/chains/2026-10-01/KO.json.gz").exists()

    def test_clock_evening_pre_close_feed_exits_three(
        self, tmp_path: Path, static_calendar
    ) -> None:
        rc = self._run(
            tmp_path,
            static_calendar,
            argv=["record-chains", "--clock", "evening", "--symbols", "KO"],
            bodies={"KO": _evening_payload("KO", ts_utc="2026-10-01 19:30:00")},
            now=datetime(2026, 10, 1, 18, 10, tzinfo=ET),
        )
        assert rc == 3
        assert not (tmp_path / "store/chains/clock=evening/2026-10-01/KO.json.gz").exists()

    def test_clock_evening_non_session_day_refused(
        self, tmp_path: Path, static_calendar
    ) -> None:
        rc = self._run(
            tmp_path,
            static_calendar,
            argv=["record-chains", "--clock", "evening"],
            bodies={},
            now=datetime(2026, 10, 3, 18, 10, tzinfo=ET),  # Saturday
        )
        assert rc == 2
