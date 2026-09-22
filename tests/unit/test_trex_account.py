"""C2: the account.json payload module (pure, broker-free)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from tree_options.trex.account import (
    AccountSnapshot,
    account_age_seconds,
    freshest,
    load_account,
    write_account,
)

ET = ZoneInfo("America/New_York")


def _snap(ts: datetime, nlv: str = "12345.67", account: str = "DUT143714") -> AccountSnapshot:
    return AccountSnapshot(
        account_id=account,
        net_liquidation=Decimal(nlv),
        cash=Decimal("11111.11"),
        buying_power=Decimal("22222.22"),
        currency="USD",
        ts=ts,
    )


class TestAccountPayload:
    def test_roundtrip_preserves_decimal_strings_and_ts(self, tmp_path: Path) -> None:
        ts = datetime(2026, 9, 22, 14, 0, tzinfo=ET)
        path = tmp_path / "account.json"
        write_account(path, _snap(ts))

        raw = json.loads(path.read_text())
        assert raw["net_liquidation"] == "12345.67"  # Decimal-as-string, repo convention
        assert raw["account_id"] == "DUT143714"
        assert raw["currency"] == "USD"
        assert raw["ts"] == ts.isoformat()
        assert not list(tmp_path.glob("*.tmp"))  # atomic: no temp left behind

        loaded = load_account(path)
        assert loaded is not None
        snap = AccountSnapshot.from_payload(loaded)
        assert snap.net_liquidation == Decimal("12345.67")
        assert snap.ts == ts

    def test_load_corrupt_or_missing_returns_none(self, tmp_path: Path) -> None:
        assert load_account(tmp_path / "nope.json") is None
        bad = tmp_path / "account.json"
        bad.write_text("{not json")
        assert load_account(bad) is None

    def test_age_seconds_negative_or_junk_is_none(self) -> None:
        future = {"ts": (datetime.now(ET) + timedelta(hours=1)).isoformat()}
        payload = {"ts": datetime.now(ET).isoformat()}
        assert account_age_seconds(payload) is not None
        assert account_age_seconds(future) == 0  # clock skew clamps, never negative
        assert account_age_seconds({}) is None
        assert account_age_seconds({"ts": "junk"}) is None


class TestFreshest:
    def test_picks_max_ts(self, tmp_path: Path) -> None:
        old = tmp_path / "a"
        new = tmp_path / "b"
        old.mkdir()
        new.mkdir()
        write_account(old / "account.json", _snap(datetime(2026, 9, 22, 10, 0, tzinfo=ET)))
        write_account(new / "account.json", _snap(datetime(2026, 9, 22, 14, 0, tzinfo=ET)))

        winner = freshest([old / "account.json", new / "account.json"])
        assert winner is not None
        path, payload = winner
        assert path == new / "account.json"
        assert payload["net_liquidation"] == "12345.67"

    def test_ignores_unreadable_and_missing(self, tmp_path: Path) -> None:
        good = tmp_path / "a"
        good.mkdir()
        write_account(good / "account.json", _snap(datetime.now(ET)))
        junk = tmp_path / "junk.json"
        junk.write_text("nope")

        winner = freshest([tmp_path / "missing.json", junk, good / "account.json"])
        assert winner is not None
        assert winner[0] == good / "account.json"

    def test_empty_input_is_none(self, tmp_path: Path) -> None:
        assert freshest([]) is None
        assert freshest([tmp_path / "never.json"]) is None

    def test_same_account_multiple_sources_picks_freshest(self, tmp_path: Path) -> None:
        """The monitor and the discovery runner both write account.json;
        the reader takes the freshest copy of the same account."""
        monitor = tmp_path / "m"
        discovery = tmp_path / "d"
        monitor.mkdir()
        discovery.mkdir()
        write_account(monitor / "account.json", _snap(datetime(2026, 9, 22, 12, 0, tzinfo=ET)))
        write_account(discovery / "account.json", _snap(datetime(2026, 9, 22, 12, 1, tzinfo=ET)))

        winner = freshest([monitor / "account.json", discovery / "account.json"])
        assert winner is not None
        assert winner[0].parent == discovery
