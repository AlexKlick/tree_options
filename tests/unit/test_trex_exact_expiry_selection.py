"""Pure selector TDD: exact expiration bypasses nearest-12 truncation."""
from __future__ import annotations

import pytest

from tree_options.trex_web.expiry_filter import select_expiries


def test_default_selection_preserves_sorted_cap() -> None:
    names = {"2027-06-18": 1, "2026-12-18": 1, "2026-11-20": 1}
    assert select_expiries(names, max_expiries=2) == ("2026-11-20", "2026-12-18")


def test_target_bypasses_twelve_without_fabricating() -> None:
    names = {f"2027-{m:02d}-19": 1 for m in range(1, 13)}
    names.update({"2028-01-21": 1, "2028-06-16": 1})
    assert select_expiries(names, max_expiries=12, expiration="2028-01-21") == ("2028-01-21",)
    assert select_expiries(names, max_expiries=12, expiration="2028-12-15") == ()


def test_live_compact_keys_select_far_expiry() -> None:
    names = {"20261120": {}, "20270115": {}, "20280915": {}}
    assert select_expiries(names, max_expiries=1, expiration="2028-09-15", compact=True) == ("20280915",)
    assert select_expiries(names, max_expiries=1, expiration="2028-09-22", compact=True) == ()


def test_reject_malformed_date_and_invalid_cap() -> None:
    with pytest.raises(ValueError):
        select_expiries({"20261218": 1}, max_expiries=0)
    with pytest.raises(ValueError):
        select_expiries({"20261218": 1}, max_expiries=1, expiration="2028-2-3")
    with pytest.raises(ValueError):
        select_expiries({"20261218": 1}, max_expiries=1, expiration="2028-02-30")
