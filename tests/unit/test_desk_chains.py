"""Desk D1 core: OCC parsing, the CBOE delayed-chain parser, the columnar
chain store (session validation, idempotency, conflicts, gaps, raw
retention) and the ``record-chains`` CLI.

No network: every fetch goes through an injected transport. Every store /
state path is pinned to tmp by the autouse fixture (a past incident leaked
host state into tests).
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.fixtures.desk_cboe import chain_payload, default_rows, encode, option_row
from tree_options.desk import chains, paths, sessions, store, universe
from tree_options.desk.__main__ import run_cli
from tree_options.trex.discovery.market import _parse_occ, parse_occ

ET = ZoneInfo("America/New_York")
D = date(2026, 9, 22)  # Tuesday session
NOW = datetime(2026, 9, 23, 6, 30, tzinfo=ET)


@pytest.fixture(autouse=True)
def _pinned_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DESK_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("TREX_DESK_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("DESK_PAPER_DIR", str(tmp_path / "paper"))
    monkeypatch.setenv("TREX_NOTIFY_ENV", str(tmp_path / "no-notify.env"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DESK_REPO_ROOT", raising=False)


def _transport(bodies: dict[str, bytes | Exception], calls: list[str] | None = None):
    """url -> body by symbol; an Exception value raises."""

    def t(url: str, *, timeout: float = 30.0) -> tuple[int, bytes]:
        if calls is not None:
            calls.append(url)
        sym = url.rsplit("/", 1)[1].removesuffix(".json")
        body = bodies.get(sym)
        if body is None:
            return 404, b"not found"
        if isinstance(body, Exception):
            raise body
        return 200, body

    return t


def _record(
    tmp_path: Path,
    bodies: dict[str, bytes | Exception],
    *,
    session: date = D,
    static_calendar,
    dry_run: bool = False,
    recheck: bool = False,
    calls: list[str] | None = None,
) -> store.RunSummary:
    return store.record_session(
        session,
        sorted(bodies),
        store=store.ChainStore(tmp_path / "store"),
        transport=_transport(bodies, calls),
        clock=lambda: NOW,
        sleep=lambda _s: None,
        cal=static_calendar,
        dry_run=dry_run,
        recheck=recheck,
    )


# ---------------------------------------------------------------- parse_occ


class TestParseOcc:
    def test_both_rights_with_exact_decimal_strikes(self) -> None:
        assert parse_occ("KO260925C00045000") == (date(2026, 9, 25), "C", Decimal("45"))
        assert parse_occ("SPY261218P00587500") == (date(2026, 12, 18), "P", Decimal("587.5"))
        assert parse_occ("F261120C00012125") == (date(2026, 11, 20), "C", Decimal("12.125"))
        # adjusted roots carry a digit; the root is everything before the tail
        assert parse_occ("KO1261120P00062500") == (date(2026, 11, 20), "P", Decimal("62.5"))

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "KO260925C0004500",  # 15 chars
            "KO260925X00045000",  # right
            "KO261325C00045000",  # month 13
            "KO260931C00045000",  # Sep 31
            "KO26A925C00045000",  # non-digit date
            "KO260925C0004500A",  # non-digit strike
            "KO260925C+0045000",  # int() would accept a sign
            "260925C00045000X",  # tail misaligned
        ],
    )
    def test_malformed_raises(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_occ(bad)

    def test_legacy_puts_only_wrapper_unchanged(self) -> None:
        assert _parse_occ("SPY261218P00575000") == ("20261218", 575.0)
        assert _parse_occ("SPY261218C00575000") is None
        assert _parse_occ("SPY") is None
        assert _parse_occ("KO1261120P00062500") == ("20261120", 62.5)

    @pytest.mark.parametrize("raw", ["00000500", "00012125", "00587500", "01234567", "99999999"])
    def test_legacy_float_strike_matches_old_formula(self, raw: str) -> None:
        # the pre-desk implementation: int(strike_raw) / 1000.0
        parsed = _parse_occ(f"SPY261218P{raw}")
        assert parsed is not None
        assert parsed[1] == int(raw) / 1000.0


# ------------------------------------------------------------- parse_chain


class TestParseChain:
    def test_header_underlying_and_columns(self) -> None:
        parsed = chains.parse_chain(encode(chain_payload()), "KO")
        assert parsed.underlying == "KO"
        # naive payload timestamp is UTC: 03:49:33Z = 23:49:33 EDT the day before
        assert parsed.source_as_of == datetime(2026, 9, 23, 3, 49, 33, tzinfo=UTC)
        uq = parsed.underlying_quote
        assert uq["close"] == 61.25 and uq["iv30"] == 18.037 and uq["volume"] == 17268957
        assert uq["last_trade_time"] == "2026-09-22T15:59:59-04:00"
        assert set(parsed.columns) == set(chains.COLUMNS)
        n = parsed.n
        assert n == 4 and all(len(v) == n for v in parsed.columns.values())
        # sorted by (exp, right, strike, occ); both rights kept
        assert parsed.columns["occ"] == [
            "KO261016C00060000",
            "KO261016P00060000",
            "KO261120C00062500",
            "KO261120P00062500",
        ]
        assert parsed.columns["exp"][0] == "2026-10-16"
        assert parsed.columns["right"] == ["C", "P", "C", "P"]
        assert parsed.columns["strike"] == [60.0, 60.0, 62.5, 62.5]
        assert parsed.columns["bid_size"] == [12, 12, 12, 12]  # float sizes -> int
        assert parsed.columns["oi"] == [120, 120, 120, 120]
        assert parsed.columns["last_time"][0] == "2026-09-22T15:30:00-04:00"
        assert parsed.n_skipped == 0

    def test_untraded_rows_and_junk_occ(self) -> None:
        rows = [
            *default_rows(),
            option_row("KO261016P00055000", bid=0.0, ask=0.01, last_time=None),
            option_row("NOT-AN-OCC"),
        ]
        payload = chain_payload(rows=rows)
        payload["data"]["options"][0]["delta"] = None  # missing greek stays None
        parsed = chains.parse_chain(encode(payload), "KO")
        assert parsed.n == 5 and parsed.n_skipped == 1
        i = parsed.columns["occ"].index("KO261016P00055000")
        assert parsed.columns["last_time"][i] is None
        assert parsed.columns["last"][i] == 0.0
        assert None in parsed.columns["delta"]

    def test_symbol_mismatch_and_missing_timestamp_refused(self) -> None:
        with pytest.raises(chains.ChainParseError):
            chains.parse_chain(encode(chain_payload("PEP")), "KO")
        with pytest.raises(chains.ChainParseError):
            chains.parse_chain(encode(chain_payload(timestamp=None)), "KO")
        with pytest.raises(chains.ChainParseError):
            chains.parse_chain(b"{torn", "KO")

    def test_url_and_index_underscore(self) -> None:
        assert chains.chain_url("KO").endswith("/delayed_quotes/options/KO.json")
        assert chains.cboe_symbol("SPX") == "_SPX"
        assert chains.cboe_symbol("SPY") == "SPY"

    def test_fetch_raw_status_and_size(self) -> None:
        assert chains.fetch_raw("KO", _transport({"KO": b"{}"})) == b"{}"
        with pytest.raises(chains.ChainFetchError):
            chains.fetch_raw("PEP", _transport({}))  # 404


# ---------------------------------------------------------------- validation


class TestValidate:
    def _v(self, **kw) -> store.Verdict:
        return store.validate(chains.parse_chain(encode(chain_payload(**kw)), "KO"), D)

    def test_ok(self) -> None:
        v = self._v()
        assert v.status == "ok" and v.payload_session == D

    def test_publish_cutoff_is_1615_et_inclusive(self) -> None:
        # 20:15:00Z = 16:15:00 EDT on D
        assert self._v(timestamp="2026-09-22 20:15:00").status == "ok"
        assert self._v(timestamp="2026-09-22 20:14:59").status == "stale"

    def test_prior_session_payload_is_stale(self) -> None:
        v = self._v(session="2026-09-21", timestamp="2026-09-22 03:40:00")
        assert v.status == "stale" and v.payload_session == date(2026, 9, 21)

    def test_payload_past_the_session_is_missing(self) -> None:
        v = self._v(session="2026-09-23", timestamp="2026-09-24 03:40:00")
        assert v.status == "missing"

    def test_bid_coverage_floor(self) -> None:
        rows = default_rows()
        for r in rows[:2]:
            r["bid"] = 0.0
        assert self._v(rows=rows).status == "ok"  # exactly 50% passes
        rows[2]["bid"] = 0.0
        assert self._v(rows=rows).status == "invalid"
        assert self._v(rows=[]).status == "invalid"

    def test_modal_date_wins_and_underlying_fallback(self) -> None:
        rows = default_rows()
        rows[0]["last_trade_time"] = "2026-09-18T11:00:00"  # 1 stale vs 3 on D
        assert self._v(rows=rows).status == "ok"
        for r in rows:
            r["last_trade_time"] = None
        # no traded rows: the underlying's last trade decides
        assert self._v(rows=rows).status == "ok"
        assert self._v(rows=rows, underlying_last="2026-09-21T15:59:59").status == "stale"


# -------------------------------------------------------------------- store


class TestStore:
    def test_ok_writes_columnar_gz_raw_and_manifest(self, tmp_path: Path, static_calendar) -> None:
        raw = encode(chain_payload())
        summary = _record(tmp_path, {"KO": raw}, static_calendar=static_calendar)
        assert summary.results["KO"].status == "ok"
        root = tmp_path / "store"
        doc = store.read_chain(root / "chains" / "2026-09-22" / "KO.json.gz")
        h = doc["header"]
        assert h["schema"] == "desk-chain/1"
        assert h["session"] == "2026-09-22" and h["underlying"] == "KO"
        assert h["source"] == "cboe-delayed"
        assert h["source_as_of"] == "2026-09-23T03:49:33+00:00"
        assert h["fetched_at"] == NOW.isoformat()
        assert h["raw_sha256"] == hashlib.sha256(raw).hexdigest()
        assert h["n"] == 4 and h["underlying_quote"]["close"] == 61.25
        assert all(len(col) == 4 for col in doc["columns"].values())
        text = gzip.decompress((root / "chains/2026-09-22/KO.json.gz").read_bytes()).decode()
        assert '"bid":[2.1,' in text.replace(" ", "")  # JSON numbers, not strings
        assert gzip.decompress((root / "raw/2026-09-22/KO.json.gz").read_bytes()) == raw
        manifest = json.loads((root / "manifest" / "2026-09-22.json").read_text())
        assert manifest["symbols"]["KO"]["status"] == "ok"
        assert manifest["symbols"]["KO"]["n"] == 4
        assert not (root / "gaps.jsonl").exists()
        assert not list(root.rglob("*.tmp"))

    def test_existing_is_never_refetched_or_rewritten(
        self, tmp_path: Path, static_calendar
    ) -> None:
        _record(tmp_path, {"KO": encode(chain_payload())}, static_calendar=static_calendar)
        path = tmp_path / "store/chains/2026-09-22/KO.json.gz"
        before = path.read_bytes()
        calls: list[str] = []
        s = _record(
            tmp_path, {"KO": encode(chain_payload())}, static_calendar=static_calendar, calls=calls
        )
        assert calls == [] and s.results["KO"].status == "exists"
        assert path.read_bytes() == before
        manifest = json.loads((tmp_path / "store/manifest/2026-09-22.json").read_text())
        assert manifest["symbols"]["KO"]["status"] == "ok"  # "exists" never downgrades ok

    def test_recheck_same_hash_exists_different_hash_conflict(
        self, tmp_path: Path, static_calendar
    ) -> None:
        _record(tmp_path, {"KO": encode(chain_payload())}, static_calendar=static_calendar)
        path = tmp_path / "store/chains/2026-09-22/KO.json.gz"
        before = path.read_bytes()
        s = _record(
            tmp_path, {"KO": encode(chain_payload())}, static_calendar=static_calendar, recheck=True
        )
        assert s.results["KO"].status == "exists"
        changed = chain_payload()
        changed["data"]["options"][0]["bid"] = 0.51
        s = _record(
            tmp_path, {"KO": encode(changed)}, static_calendar=static_calendar, recheck=True
        )
        assert s.results["KO"].status == "conflict"
        assert path.read_bytes() == before  # the original is never rewritten
        conflict = store.read_chain(tmp_path / "store/chains/2026-09-22/KO.conflict.json.gz")
        assert conflict["header"]["raw_sha256"] == hashlib.sha256(encode(changed)).hexdigest()
        gaps = [json.loads(x) for x in (tmp_path / "store/gaps.jsonl").read_text().splitlines()]
        assert [(g["session"], g["sym"], g["status"]) for g in gaps] == [
            ("2026-09-22", "KO", "conflict")
        ]

    def test_stale_writes_nothing_but_the_manifest(self, tmp_path: Path, static_calendar) -> None:
        stale = encode(chain_payload(session="2026-09-21", timestamp="2026-09-22 03:40:00"))
        s = _record(tmp_path, {"KO": stale}, static_calendar=static_calendar)
        assert s.results["KO"].status == "stale"
        assert store.exit_code(s) == 3
        root = tmp_path / "store"
        assert not (root / "chains").exists() and not (root / "raw").exists()
        manifest = json.loads((root / "manifest/2026-09-22.json").read_text())
        assert manifest["symbols"]["KO"]["status"] == "stale"
        assert not (root / "gaps.jsonl").exists()  # stale is transient, not a gap

    def test_per_symbol_error_isolation(self, tmp_path: Path, static_calendar) -> None:
        bodies: dict[str, bytes | Exception] = {
            "KO": encode(chain_payload()),
            "PEP": TimeoutError("read timed out"),
            "PG": b"{torn",
        }
        s = _record(tmp_path, bodies, static_calendar=static_calendar)
        assert s.results["KO"].status == "ok"
        assert s.results["PEP"].status == "error"
        assert "TimeoutError" in s.results["PEP"].detail
        assert s.results["PG"].status == "invalid"
        gaps = [json.loads(x) for x in (tmp_path / "store/gaps.jsonl").read_text().splitlines()]
        assert {g["sym"]: g["status"] for g in gaps} == {"PEP": "error", "PG": "invalid"}

    def test_dry_run_writes_nothing(self, tmp_path: Path, static_calendar) -> None:
        s = _record(
            tmp_path, {"KO": encode(chain_payload())}, static_calendar=static_calendar, dry_run=True
        )
        assert s.results["KO"].status == "ok"
        assert not (tmp_path / "store").exists()

    def test_raw_retention_keeps_the_newest_20_sessions(
        self, tmp_path: Path, static_calendar
    ) -> None:
        raw_root = tmp_path / "store" / "raw"
        old = static_calendar.sessions()
        i = static_calendar.ordinal(D)
        for s in old[i - 22 : i]:
            (raw_root / s.isoformat()).mkdir(parents=True)
        (raw_root / "not-a-date").mkdir()
        _record(tmp_path, {"KO": encode(chain_payload())}, static_calendar=static_calendar)
        kept = sorted(p.name for p in raw_root.iterdir())
        assert len([k for k in kept if k != "not-a-date"]) == store.RAW_KEEP_SESSIONS
        assert kept[-2] == "2026-09-22" and "not-a-date" in kept
        assert old[i - 22].isoformat() not in kept

    def test_prior_session_left_unrecorded_becomes_a_gap_once(
        self, tmp_path: Path, static_calendar
    ) -> None:
        prior = date(2026, 9, 21)
        stale = encode(chain_payload(session="2026-09-18", timestamp="2026-09-19 03:40:00"))
        _record(tmp_path, {"KO": stale}, session=prior, static_calendar=static_calendar)
        for _ in range(2):
            _record(tmp_path, {"KO": encode(chain_payload())}, static_calendar=static_calendar)
        gaps = [json.loads(x) for x in (tmp_path / "store/gaps.jsonl").read_text().splitlines()]
        assert [(g["session"], g["sym"], g["status"]) for g in gaps] == [
            ("2026-09-21", "KO", "missing")
        ]
        m = json.loads((tmp_path / "store/manifest/2026-09-21.json").read_text())
        assert m["symbols"]["KO"]["status"] == "missing"

    def test_sessions_without_any_run_are_gaps(self, tmp_path: Path, static_calendar) -> None:
        _record(
            tmp_path,
            {"KO": encode(chain_payload(session="2026-09-17", timestamp="2026-09-18 03:40:00"))},
            session=date(2026, 9, 17),
            static_calendar=static_calendar,
        )
        _record(tmp_path, {"KO": encode(chain_payload())}, static_calendar=static_calendar)
        gaps = [json.loads(x) for x in (tmp_path / "store/gaps.jsonl").read_text().splitlines()]
        assert [(g["session"], g["sym"]) for g in gaps] == [
            ("2026-09-18", "*"),
            ("2026-09-21", "*"),
        ]

    def test_exit_code_policy(self) -> None:
        def summary(**counts: int) -> store.RunSummary:
            results = {}
            for status, k in counts.items():
                for j in range(k):
                    results[f"{status}{j}"] = store.SymbolResult(status, 0, None, "")
            return store.RunSummary(D, results)

        assert store.exit_code(summary(ok=34, error=1)) == 0
        assert store.exit_code(summary(ok=30, exists=2, conflict=1, error=2)) == 0
        assert store.exit_code(summary(ok=30, stale=5)) == 3
        assert store.exit_code(summary(ok=30, error=5)) == 1
        assert store.exit_code(summary()) == 1


# ------------------------------------------------------ sessions / universe


class TestSessionsAndUniverse:
    @pytest.mark.parametrize(
        ("now", "want"),
        [
            (datetime(2026, 9, 23, 16, 14, tzinfo=ET), date(2026, 9, 22)),
            (datetime(2026, 9, 23, 16, 15, tzinfo=ET), date(2026, 9, 23)),
            (datetime(2026, 9, 26, 6, 30, tzinfo=ET), date(2026, 9, 25)),  # Saturday
            (datetime(2026, 9, 28, 6, 30, tzinfo=ET), date(2026, 9, 25)),  # Monday
            (datetime(2026, 11, 26, 18, 0, tzinfo=ET), date(2026, 11, 25)),  # Thanksgiving
            (datetime(2026, 9, 23, 22, 0, tzinfo=UTC), date(2026, 9, 23)),  # 18:00 EDT
        ],
    )
    def test_latest_completed_session(self, now: datetime, want: date, static_calendar) -> None:
        assert sessions.latest_completed_session(now, static_calendar) == want

    def test_session_helpers(self, static_calendar) -> None:
        cal = static_calendar
        assert sessions.previous_session(date(2026, 9, 21), cal) == date(2026, 9, 18)
        assert sessions.first_session_after(date(2026, 9, 24), cal) == date(2026, 9, 25)
        assert sessions.first_session_after(date(2026, 4, 24), cal) == date(2026, 4, 27)
        assert sessions.sessions_after(date(2026, 9, 17), date(2026, 9, 22), cal) == [
            date(2026, 9, 18),
            date(2026, 9, 21),
            date(2026, 9, 22),
        ]
        assert sessions.is_first_session_of_month(date(2026, 10, 1), cal)
        assert sessions.is_first_session_of_month(date(2026, 6, 1), cal)
        assert not sessions.is_first_session_of_month(date(2026, 9, 22), cal)
        assert sessions.calendar_days_between("2026-09-18", "2026-09-28") == 10
        assert sessions.cutoff_instant(D) == datetime(2026, 9, 22, 16, 15, tzinfo=ET)

    def test_universe(self) -> None:
        assert len(universe.PANEL_NAMES) == 37 == len(set(universe.PANEL_NAMES))
        assert set(universe.CHAIN_UNIVERSE) == set(universe.PANEL_NAMES) - {"TQQQ", "SQQQ"}
        assert len(universe.CHAIN_UNIVERSE) == 35
        assert len(universe.XSMOM_TRADABLES) == 36 and "SPY" not in universe.XSMOM_TRADABLES

    def test_paths_env_and_defaults(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        assert paths.store_root() == tmp_path / "store"
        assert paths.state_root() == tmp_path / "state"
        assert paths.paper_dir() == tmp_path / "paper"
        for var in ("DESK_STORE", "TREX_DESK_STATE", "DESK_PAPER_DIR", "TREX_NOTIFY_ENV"):
            monkeypatch.delenv(var)
        repo = Path(__file__).resolve().parents[2]
        assert paths.repo_root() == repo
        assert paths.store_root() == repo / "artifacts" / "desk-store"
        assert paths.paper_dir() == repo / "artifacts" / "paper-trades"
        assert paths.state_root() == tmp_path / "home" / ".local" / "state" / "trex-desk"
        assert paths.notify_env_path() == tmp_path / "home" / ".config" / "trex" / "notify.env"


# ---------------------------------------------------------------------- CLI


class TestRecordChainsCli:
    def test_summary_line_and_exit_codes(
        self, tmp_path: Path, static_calendar, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bodies = {"KO": encode(chain_payload()), "PEP": encode(chain_payload("PEP"))}
        rc = run_cli(
            ["record-chains", "--session", "2026-09-22", "--symbols", "KO,PEP"],
            transport=_transport(bodies),
            sleep=lambda _s: None,
            now=NOW,
            cal=static_calendar,
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "record-chains session=2026-09-22 symbols=2 ok=2" in out
        assert (tmp_path / "store/chains/2026-09-22/PEP.json.gz").exists()

    def test_default_session_and_stale_exit_3(self, tmp_path: Path, static_calendar) -> None:
        stale = {"KO": encode(chain_payload(session="2026-09-21", timestamp="2026-09-22 03:40"))}
        rc = run_cli(
            ["record-chains", "--symbols", "KO"],
            transport=_transport(stale),
            sleep=lambda _s: None,
            now=datetime(2026, 9, 22, 17, 45, tzinfo=ET),
            cal=static_calendar,
        )
        assert rc == 3
        assert (tmp_path / "store/manifest/2026-09-22.json").exists()

    def test_polite_pacing_between_requests(self, static_calendar) -> None:
        slept: list[float] = []
        bodies = {s: encode(chain_payload(s)) for s in ("KO", "PEP", "PG")}
        run_cli(
            ["record-chains", "--session", "2026-09-22", "--symbols", "KO,PEP,PG"],
            transport=_transport(bodies),
            sleep=slept.append,
            now=NOW,
            cal=static_calendar,
        )
        assert slept == [1.0, 1.0]

    def test_rejects_non_session_and_bad_symbols(self, static_calendar) -> None:
        kw = {
            "transport": _transport({}),
            "sleep": lambda _s: None,
            "now": NOW,
            "cal": static_calendar,
        }
        assert run_cli(["record-chains", "--session", "2026-09-26"], **kw) == 2  # Saturday
        assert run_cli(["record-chains", "--session", "2026-09-23"], **kw) == 2  # not closed
        assert run_cli(["record-chains", "--session", "2026-09-22", "--symbols", "k o"], **kw) == 2

    def test_dry_run_cli(self, tmp_path: Path, static_calendar) -> None:
        rc = run_cli(
            ["record-chains", "--session", "2026-09-22", "--symbols", "KO", "--dry-run"],
            transport=_transport({"KO": encode(chain_payload())}),
            sleep=lambda _s: None,
            now=NOW,
            cal=static_calendar,
        )
        assert rc == 0 and not (tmp_path / "store").exists()

    def test_concurrent_run_is_refused_with_exit_3(self, tmp_path: Path, static_calendar) -> None:
        import fcntl

        lock = tmp_path / "state" / "locks" / "record-chains.lock"
        lock.parent.mkdir(parents=True)
        calls: list[str] = []
        with open(lock, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            rc = run_cli(
                ["record-chains", "--session", "2026-09-22", "--symbols", "KO"],
                transport=_transport({"KO": encode(chain_payload())}, calls),
                sleep=lambda _s: None,
                now=NOW,
                cal=static_calendar,
            )
        assert rc == 3 and calls == [] and not (tmp_path / "store").exists()
