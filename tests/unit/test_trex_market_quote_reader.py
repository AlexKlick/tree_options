"""Red-first tests for unchanged, read-only TREX quote source selection."""
import json
from pathlib import Path

import pytest

from tree_options.trex_web.market_quote import read_market_quote, validate_quote_symbol


def test_snapshot_wins_over_fallback_and_never_calls_it(tmp_path: Path) -> None:
    snapshot = tmp_path / "market.json"
    original = {"last": 123.45, "source_as_of": "2026-11-10T12:05:00-05:00"}
    snapshot.write_text(json.dumps({"symbols": {"TSLA": original}}))

    def unexpected():
        raise AssertionError("cache was read despite an authoritative quote")

    assert read_market_quote(snapshot, "TSLA", unexpected) == original


def test_fallback_after_missing_file(tmp_path: Path) -> None:
    calls = []

    def fallback():
        calls.append(True)
        return {"last": 25.0}

    assert read_market_quote(tmp_path / "absent.json", "TSLA", fallback) == {"last": 25.0}
    assert calls == [True]


@pytest.mark.parametrize("contents", [
    "{bad", "[]", "null", "{}", '{"symbols": []}',
    '{"symbols": {"TSLA": "invalid"}}',
])
def test_malformed_snapshot_falls_back_without_invention(
    tmp_path: Path, contents: str
) -> None:
    p = tmp_path / "market.json"
    p.write_text(contents)
    assert read_market_quote(p, "TSLA", lambda: None) is None


def test_unrelated_symbols_do_not_bleed_into_requested_name(tmp_path: Path) -> None:
    p = tmp_path / "market.json"
    p.write_text(json.dumps({"symbols": {
        "TSLA": {"last": 123}, "NVDA": {"last": 999},
    }}))
    assert read_market_quote(p, "NVDA", lambda: {"last": 1}) == {"last": 999}
    assert read_market_quote(p, "AAPL", lambda: {"last": 5}) == {"last": 5}


def test_poison_fallback_is_null_not_an_unexpected_raw_object(tmp_path: Path) -> None:
    assert read_market_quote(tmp_path / "absent.json", "TSLA", lambda: "poison") is None


@pytest.mark.parametrize("raw,expected", [
    ("tsla", "TSLA"), ("Brk.B", "BRK.B"), ("F", "F"),
])
def test_canonical_symbol(raw: str, expected: str) -> None:
    assert validate_quote_symbol(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "A..B", "A.B.C", "ABCDEFG", "../../commands",
    "TSLA/quote", None, 4,
])
def test_noncanonical_symbol_denied(raw: object) -> None:
    with pytest.raises(ValueError):
        validate_quote_symbol(raw)
