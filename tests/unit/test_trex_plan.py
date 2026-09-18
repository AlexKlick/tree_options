"""trex plan model tests: validation fails closed, money stays Decimal."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tree_options.trex.plan import PutSpread, TradePlan, load_plan

VALID_TOML = """
id = "putspread-20260918"
account_mode = "paper"
total_debit_cap = 1840.00
entry_window_start = "09:45"
entry_window_end = "12:00"

[[structures]]
id = "nvda-oct"
underlying = "NVDA"
entry_date = 2026-09-18
expiry = 2026-10-16
long_strike = 185.0
short_strike = 150.0
quantity = 5
limit_cap = 0.50
exit_deadline = 2026-10-09

[[structures]]
id = "qqq-nov"
underlying = "QQQ"
entry_date = 2026-09-18
expiry = 2026-11-20
long_strike = 600.0
short_strike = 475.0
quantity = 4
limit_cap = 2.40
exit_deadline = 2026-11-06
"""


def _spread(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "t",
        "underlying": "NVDA",
        "entry_date": "2026-09-18",
        "expiry": "2026-10-16",
        "long_strike": "185",
        "short_strike": "150",
        "quantity": 1,
        "limit_cap": "0.50",
        "exit_deadline": "2026-10-09",
    }
    base.update(overrides)
    return base


def _plan(structures: list[dict[str, object]], **overrides: object) -> TradePlan:
    kwargs: dict[str, object] = {
        "id": "p",
        "account_mode": "paper",
        "structures": structures,
        "total_debit_cap": "10000",
        "entry_window_start": "09:45",
        "entry_window_end": "12:00",
    }
    kwargs.update(overrides)
    return TradePlan(**kwargs)


class TestSpreadValidation:
    def test_valid_spread_derived_math(self) -> None:
        s = PutSpread(**_spread(quantity=5))
        assert s.width == Decimal("35")
        assert s.max_debit == Decimal("250.00")
        assert s.max_value == Decimal("17500")

    def test_toml_floats_become_exact_decimals(self) -> None:
        s = PutSpread(**_spread(limit_cap=0.10))
        assert s.limit_cap == Decimal("0.1")

    def test_inverted_strikes_rejected(self) -> None:
        with pytest.raises(ValueError, match="long strike"):
            PutSpread(**_spread(long_strike="100", short_strike="150"))

    def test_deadline_on_expiry_rejected(self) -> None:
        with pytest.raises(ValueError, match="never hold to expiry"):
            PutSpread(**_spread(exit_deadline="2026-10-16"))

    def test_entry_on_deadline_rejected(self) -> None:
        with pytest.raises(ValueError, match="precede exit deadline"):
            PutSpread(**_spread(entry_date="2026-10-09"))


class TestBookValidation:
    def test_duplicate_ids_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            _plan([_spread(), _spread(id="t")])

    def test_book_cap_exceeded_rejected(self) -> None:
        with pytest.raises(ValueError, match="exceeds book cap"):
            _plan([_spread(quantity=100)], total_debit_cap="100")

    def test_live_book_above_hard_rail_rejected(self) -> None:
        with pytest.raises(ValueError, match="hard rail"):
            _plan([_spread(quantity=200)], account_mode="live")

    def test_committed_at_caps_sums(self) -> None:
        plan = _plan([_spread(quantity=5), _spread(id="u", quantity=4, limit_cap="2.40")])
        assert plan.committed_at_caps == Decimal("1210.00")


class TestLoadPlan:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "plan.toml"
        path.write_text(VALID_TOML)
        plan = load_plan(path)
        assert plan.id == "putspread-20260918"
        assert plan.account_mode == "paper"
        assert [s.id for s in plan.structures] == ["nvda-oct", "qqq-nov"]
        qqq = plan.structures[1]
        assert qqq.width == Decimal("125")
        assert plan.committed_at_caps == Decimal("1210.00")

    def test_empty_book_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.toml"
        path.write_text('id = "x"\naccount_mode = "paper"\ntotal_debit_cap = 100.0\n')
        with pytest.raises(ValueError, match="empty book"):
            load_plan(path)

    def test_filename_stem_is_default_id(self, tmp_path: Path) -> None:
        path = tmp_path / "anon.toml"
        path.write_text(
            VALID_TOML.replace('id = "putspread-20260918"\n', "")
        )
        assert load_plan(path).id == "anon"
