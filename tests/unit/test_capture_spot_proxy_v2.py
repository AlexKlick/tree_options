"""M4-B: the spot_proxy_v2 capture bridge (the owner-ruled 2026-08-29 capture).

Hermetic. There is no network here and THE REAL API KEY IS NEVER READ: every
test drives `scripts/capture_spot_proxy_v2.py` through an injected transport
and a temporary cache directory, with `TEST-KEY-NEVER-REAL` standing in for
the secret exactly as the structural-capture lane's tests do. Nothing in this
file opens `~/.config/tree_options/polygon.key` or `POLYGON_API_KEY`.

What is worth testing is the seam the brief rules:

* the universe and window come from the DECLARED inputs — the real
  `data/coverage/coverage_universe.json` (tracked) and the era's own
  `artifacts/m4b-coverage-era/spot_proxy.json` (host-only, read-only) — never
  a clock read;
* one `/v2/aggs/ticker/{underlying}/range/1/day/{start}/{end}` request per
  UNDERLYING name, on the existing client, and the produced file LOADS
  through the real `load_spot_proxy_v2` (the round trip);
* the row discipline is the loader's own (`_validated_spot_v2_row` shared at
  the mapping layer): a float close refuses, a bool/float/string/negative
  volume refuses, a zero volume is a real observation;
* the vendor's close TOKEN survives byte-exact — never through a float —
  which is what mutant M333 pins;
* a vendor gap (an era session absent from the response) fails the run
  NAMING the session and writes NOTHING, which is what mutant M334 pins;
* a session the era's own proxy never carried for that name is a legitimate,
  recorded preexisting-absent skip;
* custody carries per-name receipts, the output sha256, the set hashes, and
  `network: true`;
* outputs are confined to artifacts/ and out of the protected era subtrees.

The real-files happy path SKIPS where the era proxy is absent (the mutation
harness's disposable copy excludes artifacts/): the behavior tests below it
run on synthetic declared inputs everywhere, so the mutant owners never
depend on host-only state.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest

from tests.conftest import REPO_ROOT
from tree_options.data.massive_client import HttpResponse, MassiveClient, RateGovernor
from tree_options.data.vwap_pit_surface import load_spot_proxy_v2, repo_exchange_calendar
from tree_options.time.sessions import SESSION_TIMEZONE

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import capture_spot_proxy_v2 as cap  # type: ignore[import-not-found]  # scripts/

KEY = "TEST-KEY-NEVER-REAL"
REAL_UNIVERSE = REPO_ROOT / "data" / "coverage" / "coverage_universe.json"
REAL_ERA_PROXY = REPO_ROOT / "artifacts" / "m4b-coverage-era" / "spot_proxy.json"

SYN_D1, SYN_D2, SYN_D3 = date(2025, 4, 7), date(2025, 4, 8), date(2025, 4, 9)
# 21 significant digits: a decimal token no binary float can carry — the
# byte-exactness owner's probe (mutant M333 maps this through float and loses
# the tail).
LONG_TOKEN = "123.456789012345678901"


# ---- the vendor double (this lane's own; no other lane's file is edited) -----


class AggsVendor:
    """A URL-routed /v2/aggs double that records every LIVE call it served.

    Bodies are handed over as TEXT so the real client's `loads_exact` parses
    the number tokens exactly as it would the vendor's bytes."""

    def __init__(self, routes: dict[str, str]) -> None:
        self.routes = routes
        self.calls: list[str] = []
        self.statuses: dict[str, int] = {}

    def __call__(self, url: str, *, timeout: float) -> HttpResponse:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        assert query.pop("apiKey", None) == KEY, "the key is appended at request time"
        canonical = parts.path + ("?" + urlencode(sorted(query.items())) if query else "")
        self.calls.append(canonical)
        body = self.routes.get(canonical)
        if body is None:  # a test asked for an unrouted URL
            raise AssertionError(f"unrouted: {canonical}")
        self.statuses[parts.path] = 200
        return HttpResponse(200, body.encode("utf-8"), {})


def make_client(cache_dir: Path, vendor: AggsVendor) -> MassiveClient:
    """A client with spacing disabled — the governor is the client lane's own
    tested unit, and paying 12s per name here would buy nothing."""
    return MassiveClient(
        api_key=KEY,
        transport=vendor,
        cache_dir=cache_dir,
        governor=RateGovernor(None),
    )


def aggs_url(ticker: str, start: date, end: date) -> str:
    return (
        f"/v2/aggs/ticker/{ticker}/range/1/day/{start.isoformat()}/{end.isoformat()}?adjusted=false"
    )


def _t(session: date) -> int:
    """Millisecond epoch of midnight America/New_York on `session` (the
    vendor's daily-aggregate anchor)."""
    local = datetime(session.year, session.month, session.day, tzinfo=SESSION_TIMEZONE)
    return int(local.timestamp()) * 1000


def _bar(t: int, close_token: str, volume: Any) -> str:
    return (
        f'{{"T":"X","t":{t},"o":{close_token},"h":{close_token},"l":{close_token},'
        f'"c":{close_token},"v":{volume},"n":12}}'
    )


def _aggs_body(ticker: str, bars: list[str], *, request_id: str) -> str:
    count = len(bars)
    return (
        f'{{"adjusted":false,"count":{count},"queryCount":{count},'
        f'"request_id":"{request_id}","results":[{",".join(bars)}],'
        f'"resultsCount":{count},"status":"OK","ticker":"{ticker}"}}'
    )


def _write_universe(tmp_path: Path, names: list[str]) -> Path:
    path = tmp_path / f"universe-{uuid.uuid4().hex[:8]}.json"
    path.write_text(
        json.dumps({"schema_version": "m4-coverage-universe/2", "underlyings": names}),
        encoding="utf-8",
    )
    return path


def _write_era(tmp_path: Path, era: dict[str, list[date]]) -> Path:
    path = tmp_path / f"era-{uuid.uuid4().hex[:8]}.json"
    path.write_text(
        json.dumps(
            {
                name: {session.isoformat(): "500.00" for session in sessions}
                for name, sessions in era.items()
            }
        ),
        encoding="utf-8",
    )
    return path


def _run(
    tmp_path: Path,
    routes: dict[str, str],
    *,
    out: Path,
    custody: Path,
    universe: Path,
    era: Path,
    cache: Path | None = None,
) -> tuple[int, AggsVendor]:
    """Drive `cap.main` over a fresh vendor double; returns (exit, vendor)."""
    vendor = AggsVendor(routes)

    def factory(**kwargs: Any) -> tuple[MassiveClient, dict[str, int]]:
        return make_client(cache or tmp_path / "cache", vendor), vendor.statuses

    code = cap.main(
        [
            "--out",
            str(out),
            "--custody",
            str(custody),
            "--universe",
            str(universe),
            "--era-proxy",
            str(era),
        ],
        client_factory=factory,
    )
    return code, vendor


def _never_builds_a_client(**kwargs: Any) -> tuple[MassiveClient, dict[str, int]]:
    raise AssertionError("a refused run must never build a client")


@pytest.fixture()
def artifacts_dir() -> Iterator[Path]:
    """A throwaway directory under the repo's artifacts/ (the out-guard's
    confinement root), removed afterwards — the amendment lane's pattern."""
    root = REPO_ROOT / "artifacts" / f"spotv2-test-{uuid.uuid4().hex[:12]}"
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---- the real declared inputs (host-only era proxy; skips where absent) ------


@dataclass(frozen=True)
class RealWindow:
    names: tuple[str, ...]
    era: dict[str, frozenset[date]]
    union: tuple[date, ...]
    routes: dict[str, str]
    rows_per_name: dict[str, int]


@pytest.fixture(scope="module")
def real_window(tmp_path_factory: pytest.TempPathFactory) -> RealWindow:
    if not REAL_ERA_PROXY.exists():
        pytest.skip("the era's spot proxy is host-only (artifacts/ is excluded from clones)")
    era_raw = json.loads(REAL_ERA_PROXY.read_text(encoding="utf-8"))
    universe = json.loads(REAL_UNIVERSE.read_text(encoding="utf-8"))["underlyings"]
    era = {
        name: frozenset(date.fromisoformat(s) for s in sessions)
        for name, sessions in era_raw.items()
    }
    union = sorted(set().union(*era.values()))
    start, end = union[0], union[-1]
    sessions = tuple(s for s in repo_exchange_calendar().sessions() if start <= s <= end)

    routes: dict[str, str] = {}
    rows_per_name: dict[str, int] = {}
    for name_index, name in enumerate(universe):
        bars = []
        for i, session in enumerate(sessions):
            # an integer close every 10th session exercises the int->Decimal
            # leg of the mapping alongside the decimal-token leg
            close = f"{200 + (name_index + i) % 60}" if i % 10 == 0 else f"{200 + (i % 60)}.25"
            bars.append(_bar(_t(session), close, 1_000_000 + i))
        if name == "AAPL":  # the out-of-window filter's probes
            bars.insert(0, _bar(_t(date(2024, 8, 20)), "199.00", 111))
            bars.append(_bar(_t(date(2026, 8, 17)), "201.00", 222))
        routes[aggs_url(name, start, end)] = _aggs_body(name, bars, request_id=f"req-{name}-1")
        rows_per_name[name] = len(sessions)
    return RealWindow(
        names=tuple(universe),
        era=era,
        union=tuple(union),
        routes=routes,
        rows_per_name=rows_per_name,
    )


def test_real_files_happy_path_round_trips_through_the_loader(
    tmp_path: Path, artifacts_dir: Path, real_window: RealWindow
) -> None:
    """The declared universe + the era's own session keys drive one request
    per UNDERLYING; the file that lands is EXACTLY `load_spot_proxy_v2`'s
    shape and the real loader parses it back; era sessions are all present."""
    out = artifacts_dir / "spot-proxy-v2.json"
    custody = artifacts_dir / "spot-proxy-v2-custody.json"
    code, vendor = _run(
        tmp_path,
        real_window.routes,
        out=out,
        custody=custody,
        universe=REAL_UNIVERSE,
        era=REAL_ERA_PROXY,
    )
    assert code == 0, custody.read_text(encoding="utf-8") if custody.exists() else "no custody"
    start, end = real_window.union[0], real_window.union[-1]
    assert vendor.calls == [aggs_url(name, start, end) for name in real_window.names], (
        "one adjusted=false equity-aggregates request per underlying, in universe order"
    )

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert set(payload) == set(real_window.names)
    for name in real_window.names:
        assert set(payload[name]) >= {s.isoformat() for s in real_window.era[name]}, (
            f"{name}: every era session must be answerable"
        )
        for cell in payload[name].values():
            assert set(cell) == {"close", "volume"}
            assert isinstance(cell["close"], str)
            assert type(cell["volume"]) is int

    parsed = load_spot_proxy_v2(out)  # the round trip through the real loader
    assert set(parsed) == set(real_window.names)
    for name in real_window.names:
        for session_iso, cell in payload[name].items():
            session = date.fromisoformat(session_iso)
            assert parsed[name][session] == (Decimal(cell["close"]), cell["volume"])

    # the out-of-window probes are excluded from AAPL's rows
    assert "2024-08-20" not in payload["AAPL"] and "2026-08-17" not in payload["AAPL"]

    doc = json.loads(custody.read_text(encoding="utf-8"))
    aapl = doc["names"]["AAPL"]
    assert aapl["rows_written"] == real_window.rows_per_name["AAPL"]
    assert aapl["rows_outside_window"] == 2
    assert aapl["expected_era_sessions"] == len(real_window.era["AAPL"])
    assert doc["output"]["rows"] == sum(real_window.rows_per_name.values())


def test_real_files_output_is_deterministic_across_runs(
    tmp_path: Path, artifacts_dir: Path, real_window: RealWindow
) -> None:
    """Identical vendor bytes -> an identical output sha (the second run is
    cache-served, proving the dump is a pure function of the bodies)."""
    out = artifacts_dir / "spot-proxy-v2.json"
    custody = artifacts_dir / "spot-proxy-v2-custody.json"
    cache = tmp_path / "cache"
    first, vendor_first = _run(
        tmp_path,
        real_window.routes,
        out=out,
        custody=custody,
        universe=REAL_UNIVERSE,
        era=REAL_ERA_PROXY,
        cache=cache,
    )
    assert first == 0
    first_sha = hashlib.sha256(out.read_bytes()).hexdigest()
    assert vendor_first.calls, "the first run hit the wire"

    second, vendor_second = _run(
        tmp_path,
        real_window.routes,
        out=out,
        custody=artifacts_dir / "custody-2.json",
        universe=REAL_UNIVERSE,
        era=REAL_ERA_PROXY,
        cache=cache,
    )
    assert second == 0
    assert vendor_second.calls == [], "the second run was served from the cache"
    assert hashlib.sha256(out.read_bytes()).hexdigest() == first_sha
    doc = json.loads((artifacts_dir / "custody-2.json").read_text(encoding="utf-8"))
    assert doc["names"][real_window.names[0]]["served_from"] == "cache"


def test_real_files_custody_carries_receipts_sha_and_network_marker(
    artifacts_dir: Path, tmp_path: Path, real_window: RealWindow
) -> None:
    out = artifacts_dir / "spot-proxy-v2.json"
    custody = artifacts_dir / "spot-proxy-v2-custody.json"
    code, _ = _run(
        tmp_path,
        real_window.routes,
        out=out,
        custody=custody,
        universe=REAL_UNIVERSE,
        era=REAL_ERA_PROXY,
    )
    assert code == 0
    doc = json.loads(custody.read_text(encoding="utf-8"))
    assert doc["network"] is True
    assert doc["output"]["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert (
        doc["universe"]["set_sha256"]
        == hashlib.sha256(
            json.dumps(sorted(real_window.names), separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert (
        doc["window"]["set_sha256"]
        == hashlib.sha256(
            json.dumps([s.isoformat() for s in real_window.union], separators=(",", ":")).encode()
        ).hexdigest()
    )
    start_iso, end_iso = real_window.union[0].isoformat(), real_window.union[-1].isoformat()
    assert (doc["window"]["start"], doc["window"]["end"]) == (start_iso, end_iso)
    assert set(doc["names"]) == set(real_window.names)
    for name, receipt in doc["names"].items():
        assert receipt["endpoint"] == aggs_url(name, real_window.union[0], real_window.union[-1])
        assert receipt["http_status"] == 200
        assert receipt["served_from"] == "wire"
        assert receipt["vendor_status"] == "OK"
        assert receipt["rows_in_response"] >= receipt["rows_written"]
        assert (receipt["first_session"], receipt["last_session"]) == (start_iso, end_iso)
        assert receipt["gaps"] == []


# ---- the vendor gap: named, fatal, no partial file (mutant M334's owner) -----


def test_a_vendor_gap_fails_the_run_naming_the_session(
    tmp_path: Path, artifacts_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An era session the response cannot answer is a NAMED gap: the run
    exits non-zero and NOTHING is written (fail-closed, no partial file)."""
    universe = _write_universe(tmp_path, ["SPY", "TSLA"])
    era = _write_era(tmp_path, {"SPY": [SYN_D1, SYN_D2], "TSLA": [SYN_D1, SYN_D2]})
    routes = {
        aggs_url("SPY", SYN_D1, SYN_D2): _aggs_body(
            "SPY",
            [_bar(_t(SYN_D1), "600.00", 80_000_000), _bar(_t(SYN_D2), "601.00", 81_000_000)],
            request_id="r-spy",
        ),
        # TSLA is missing SYN_D2 entirely: the vendor gap
        aggs_url("TSLA", SYN_D1, SYN_D2): _aggs_body(
            "TSLA", [_bar(_t(SYN_D1), "270.50", 40_000_000)], request_id="r-tsla"
        ),
    }
    out = artifacts_dir / "spot-proxy-v2.json"
    custody = artifacts_dir / "spot-proxy-v2-custody.json"
    code, _ = _run(tmp_path, routes, out=out, custody=custody, universe=universe, era=era)
    assert code == 4, "an unanswerable era session is a capture refusal"
    err = capsys.readouterr().err
    assert "TSLA" in err and SYN_D2.isoformat() in err, err
    assert not out.exists() and not custody.exists(), "no partial file may land"


def test_a_preexisting_absent_session_is_a_legitimate_recorded_skip(
    tmp_path: Path, artifacts_dir: Path
) -> None:
    """A session the era's OWN proxy never carried for a name may be absent
    from that name's response: the run succeeds and custody records the skip
    as preexisting-absent — it is not a vendor gap."""
    universe = _write_universe(tmp_path, ["SPY", "TSLA"])
    era = _write_era(tmp_path, {"SPY": [SYN_D1], "TSLA": [SYN_D1, SYN_D2]})
    routes = {
        # SPY's response omits SYN_D2 — its own era proxy never carried it
        aggs_url("SPY", SYN_D1, SYN_D2): _aggs_body(
            "SPY", [_bar(_t(SYN_D1), "600.00", 80_000_000)], request_id="r-spy"
        ),
        aggs_url("TSLA", SYN_D1, SYN_D2): _aggs_body(
            "TSLA",
            [_bar(_t(SYN_D1), "270.50", 40_000_000), _bar(_t(SYN_D2), "271.50", 41_000_000)],
            request_id="r-tsla",
        ),
    }
    out = artifacts_dir / "spot-proxy-v2.json"
    custody = artifacts_dir / "spot-proxy-v2-custody.json"
    code, _ = _run(tmp_path, routes, out=out, custody=custody, universe=universe, era=era)
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert set(payload["SPY"]) == {SYN_D1.isoformat()}
    assert set(payload["TSLA"]) == {SYN_D1.isoformat(), SYN_D2.isoformat()}
    doc = json.loads(custody.read_text(encoding="utf-8"))
    assert doc["preexisting_absent"] == {"SPY": [SYN_D2.isoformat()], "TSLA": []}


# ---- the row discipline at the mapping layer ----------------------------------


def test_mapping_refuses_a_float_close() -> None:
    """A float close means exactness was already lost upstream — refused,
    never coerced (the client's `loads_exact` makes this unreachable on the
    wire; the mapping still refuses one)."""
    with pytest.raises(cap.CaptureRefusedError, match="float"):
        cap.map_vendor_row("SPY[2025-04-07]", {"t": _t(SYN_D1), "c": 226.84, "v": 5})


@pytest.mark.parametrize(
    ("volume", "complaint"),
    [
        (True, "strict int"),
        (1.5, "strict int"),
        ("7", "strict int"),
        (Decimal("-5.7"), "negative fractional volume"),
        (-5, "strict int"),
    ],
)
def test_mapping_refuses_every_loose_volume(volume: Any, complaint: str) -> None:
    row = {"t": _t(SYN_D1), "c": Decimal("600.00"), "v": volume}
    with pytest.raises(cap.CaptureRefusedError, match=complaint):
        cap.map_vendor_row("SPY[2025-04-07]", row)


def test_mapping_accepts_an_integral_decimal_volume_the_wire_shape() -> None:
    """(2026-08-31, the live AAPL refusal) the vendor SHIPS volumes as
    float-shaped JSON tokens (e.g. 50190574.0) — `loads_exact` hands those
    to the mapping as Decimal. An INTEGRAL Decimal converts exactly (the
    int is the same number, no float ever exists); a fractional one keeps
    refusing. Without this, the first real capture refused AAPL row 0
    (exit 4) and wrote nothing."""
    _session, _close_token, volume, truncated = cap.map_vendor_row(
        "AAPL[2024-09-03]",
        {"t": _t(date(2024, 9, 3)), "c": Decimal("226.84"), "v": Decimal("50190574.0")},
    )
    assert volume == 50190574
    assert type(volume) is int
    assert truncated is False


def test_mapping_truncates_a_fractional_volume_toward_zero_counted() -> None:
    """(2026-08-31, the live AAPL retry) the consolidated feed emits
    sub-share volumes (37308155.220558 on 2026-02-23). The file's contract
    is a strict int, and truncating toward zero NEVER inflates a liquidity
    measure (the $50M median's conservative direction). The truncation is
    COUNTED per name in the custody receipt — disclosed, never silent."""
    _session, _close_token, volume, truncated = cap.map_vendor_row(
        "AAPL[2026-02-23]",
        {"t": _t(date(2026, 2, 23)), "c": Decimal("270.5"), "v": Decimal("37308155.220558")},
    )
    assert volume == 37308155
    assert truncated is True


def test_mapping_accepts_a_zero_volume_as_a_real_observation() -> None:
    session, close_token, volume, truncated = cap.map_vendor_row(
        "SPY[2025-04-07]", {"t": _t(SYN_D1), "c": Decimal("600.00"), "v": 0}
    )
    assert (session, close_token, volume, truncated) == (SYN_D1, "600", 0, False)


def test_a_bool_volume_in_a_vendor_body_fails_the_run(
    tmp_path: Path, artifacts_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    universe = _write_universe(tmp_path, ["SPY"])
    era = _write_era(tmp_path, {"SPY": [SYN_D1]})
    routes = {
        aggs_url("SPY", SYN_D1, SYN_D1): (
            '{"adjusted":false,"count":1,"queryCount":1,"request_id":"r",'
            '"results":[{"T":"X","t":' + str(_t(SYN_D1)) + ',"c":600.00,"v":true,"n":1}],'
            '"resultsCount":1,"status":"OK","ticker":"SPY"}'
        )
    }
    out = artifacts_dir / "spot-proxy-v2.json"
    code, _ = _run(
        tmp_path,
        routes,
        out=out,
        custody=artifacts_dir / "c.json",
        universe=universe,
        era=era,
    )
    assert code == 4
    err = capsys.readouterr().err
    assert SYN_D1.isoformat() in err and "strict int" in err, err
    assert not out.exists()


def test_vendor_close_tokens_round_trip_byte_exact(tmp_path: Path, artifacts_dir: Path) -> None:
    """The vendor's close TOKEN is the provenance: a 21-significant-digit
    decimal must land in the file unchanged (mutant M333 maps it through a
    float and loses the tail)."""
    universe = _write_universe(tmp_path, ["SPY"])
    era = _write_era(tmp_path, {"SPY": [SYN_D1]})
    routes = {
        aggs_url("SPY", SYN_D1, SYN_D1): _aggs_body(
            "SPY", [_bar(_t(SYN_D1), LONG_TOKEN, 7)], request_id="r"
        )
    }
    out = artifacts_dir / "spot-proxy-v2.json"
    code, _ = _run(
        tmp_path,
        routes,
        out=out,
        custody=artifacts_dir / "c.json",
        universe=universe,
        era=era,
    )
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["SPY"][SYN_D1.isoformat()] == {"close": LONG_TOKEN, "volume": 7}
    assert load_spot_proxy_v2(out)["SPY"][SYN_D1] == (Decimal(LONG_TOKEN), 7)


# ---- the out-guard -------------------------------------------------------------


@pytest.mark.parametrize(
    "argv_extra",
    [
        ["--out", "/tmp/spot-proxy-v2.json"],
        ["--custody", "/tmp/spot-proxy-v2-custody.json"],
        ["--out", "artifacts/bars/spot-proxy-v2.json"],
        ["--out", "artifacts/m4b-coverage-era/spot_proxy_v2.json"],
        ["--out", "artifacts/bars-authority/spot_proxy_v2.json"],
        ["--out", "README.md"],
    ],
)
def test_output_paths_outside_artifacts_are_refused(
    tmp_path: Path, artifacts_dir: Path, argv_extra: list[str]
) -> None:
    """--out/--custody must stay under artifacts/ and out of the protected
    era subtrees; the refusal happens BEFORE any client is built, and a
    relative path resolves against the repo root, never the cwd."""
    called = False

    def factory(**kwargs: Any) -> tuple[MassiveClient, dict[str, int]]:
        nonlocal called
        called = True
        raise AssertionError("a refused run must never build a client")

    argv = [
        "--out",
        str(artifacts_dir / "out.json"),
        "--custody",
        str(artifacts_dir / "custody.json"),
        *argv_extra,
    ]
    with pytest.MonkeyPatch.context() as patches:
        patches.chdir(tmp_path)  # a cwd escape must not dodge the guard
        code = cap.main(argv, client_factory=factory)
    assert code == 5
    assert not called


def test_out_and_custody_may_not_be_the_same_file(artifacts_dir: Path) -> None:
    same = str(artifacts_dir / "same.json")
    code = cap.main(["--out", same, "--custody", same], client_factory=_never_builds_a_client)
    assert code == 5
    assert not Path(same).exists()
