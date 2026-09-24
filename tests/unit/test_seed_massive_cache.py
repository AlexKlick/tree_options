"""Seeding a NEW Massive cache from contract-master pages another cache holds.

Hermetic: two temporary cache directories, no network, no key. The oracle for
every cache file name is computed here from the documented key rule (sha256
over path + "?" + urlencode(sorted params)), never by calling the seeder.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import pytest

from tests.conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import seed_massive_cache as seed  # type: ignore[import-not-found]  # scripts/

CONTRACTS = "/v3/reference/options/contracts"
AS_OF = date(2025, 3, 14)


def key_of(path: str, params: dict[str, str]) -> str:
    canonical = path + "?" + urlencode(sorted(params.items()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def first_key(underlying: str, as_of: date) -> str:
    return key_of(
        CONTRACTS, {"underlying_ticker": underlying, "as_of": as_of.isoformat(), "limit": "1000"}
    )


CURSOR_KEY = key_of(CONTRACTS, {"cursor": "abc"})

PAGE_1 = (
    b'{"results":[{"ticker":"O:SPY250417C00560000","strike_price":560}],"status":"OK",'
    b'"request_id":"r1","next_url":"https://api.polygon.io/v3/reference/options/contracts'
    b'?cursor=abc"}'
)
PAGE_2 = b'{"results":[{"ticker":"O:SPY250417P00560000","strike_price":587.5}],"status":"OK"}'


def write(cache: Path, key: str, body: bytes) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"{key}.json"
    target.write_bytes(body)
    return target


@pytest.fixture()
def caches(tmp_path: Path) -> tuple[Path, Path]:
    src, dst = tmp_path / "src", tmp_path / "dst"
    write(src, first_key("SPY", AS_OF), PAGE_1)
    write(src, CURSOR_KEY, PAGE_2)
    return src, dst


def test_a_two_page_master_is_copied_byte_for_byte_and_the_source_untouched(
    caches: tuple[Path, Path],
) -> None:
    src, dst = caches
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in src.iterdir()}

    report = seed.seed_masters(src, dst, ["SPY"], [AS_OF])

    assert (dst / f"{first_key('SPY', AS_OF)}.json").read_bytes() == PAGE_1
    assert (dst / f"{CURSOR_KEY}.json").read_bytes() == PAGE_2
    assert report.copied == 2 and report.present == 0
    assert report.masters_complete == 1
    assert report.masters_partial == [] and report.masters_missing == []
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in src.iterdir()}
    assert after == before, "the source cache is read-only"


def test_a_rerun_copies_nothing_and_counts_what_is_present(caches: tuple[Path, Path]) -> None:
    src, dst = caches
    seed.seed_masters(src, dst, ["SPY"], [AS_OF])

    report = seed.seed_masters(src, dst, ["SPY"], [AS_OF])

    assert report.copied == 0 and report.present == 2 and report.masters_complete == 1


def test_a_master_the_source_lacks_is_named_and_nothing_is_written(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()

    report = seed.seed_masters(src, dst, ["SPY"], [AS_OF])

    assert report.masters_missing == ["SPY 2025-03-14"]
    assert report.copied == 0
    assert not dst.exists() or list(dst.iterdir()) == []


def test_a_walk_that_breaks_mid_master_keeps_the_pages_it_has(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    write(src, first_key("SPY", AS_OF), PAGE_1)  # page 2 absent

    report = seed.seed_masters(src, dst, ["SPY"], [AS_OF])

    assert report.copied == 1
    assert report.masters_partial == ["SPY 2025-03-14"]
    assert report.masters_complete == 0


@pytest.mark.parametrize(
    "bad",
    [
        b"not json",
        b'{"status":"NOT_AUTHORIZED","message":"upgrade"}',
        b'{"results":[]}',  # no status field
    ],
)
def test_an_unusable_source_page_is_never_copied(tmp_path: Path, bad: bytes) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    write(src, first_key("SPY", AS_OF), bad)

    report = seed.seed_masters(src, dst, ["SPY"], [AS_OF])

    assert report.copied == 0
    assert report.masters_missing == ["SPY 2025-03-14"]


def test_a_differing_destination_entry_is_refused_never_overwritten(
    caches: tuple[Path, Path],
) -> None:
    src, dst = caches
    theirs = b'{"results":[],"status":"OK","request_id":"other"}'
    write(dst, first_key("SPY", AS_OF), theirs)

    report = seed.seed_masters(src, dst, ["SPY"], [AS_OF])

    assert (dst / f"{first_key('SPY', AS_OF)}.json").read_bytes() == theirs
    assert report.conflicts == [f"SPY 2025-03-14 page 1 ({first_key('SPY', AS_OF)})"]


def argv(src: Path, dst: Path, underlyings: str) -> list[str]:
    return [
        "--from-cache",
        str(src),
        "--to-cache",
        str(dst),
        "--underlyings",
        underlyings,
        "--as-of",
        AS_OF.isoformat(),
    ]


def test_cli_prints_a_json_summary(
    caches: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    src, dst = caches

    rc = seed.main(argv(src, dst, "SPY,TSLA"))

    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["copied"] == 2
    assert summary["masters_complete"] == 1
    assert summary["masters_missing"] == ["TSLA 2025-03-14"]


def test_cli_exits_2_on_a_conflict(caches: tuple[Path, Path]) -> None:
    src, dst = caches
    write(dst, CURSOR_KEY, b'{"results":[],"status":"OK"}')

    assert seed.main(argv(src, dst, "SPY")) == 2


def test_cli_refuses_to_seed_a_cache_into_itself(caches: tuple[Path, Path]) -> None:
    src, _ = caches

    with pytest.raises(SystemExit) as excinfo:
        seed.main(argv(src, src / ".", "SPY"))

    assert excinfo.value.code == 2
