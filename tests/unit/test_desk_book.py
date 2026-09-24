"""Desk book adapter: the account's exposure for the rails, read-only.

The legacy trex book is read from COPIES: ``tests/fixtures/desk_rails``
holds the two plan TOMLs (from ``plans/``) and a snapshot of the live
``book.json`` files taken 2026-09-23 (two NVDA put spreads open, the QQQ
spread closed unentered; the superseded 09-18 book never entered). Every
test copies them into tmp and proves the adapter wrote nothing.

Expected max losses are computed by hand here: an open position risks
its entry fill x open quantity x 100; a working entry risks its cap.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tree_options.desk import book as desk_book
from tree_options.desk import rails
from tree_options.desk.rails import (
    FAIL,
    NOT_EVALUABLE,
    PASS,
    BookView,
    Candidate,
    CandidateLeg,
    PositionRisk,
    RailContext,
)
from tree_options.trex.plan import ExitRules, Leg, LegStructure

ET = ZoneInfo("America/New_York")
D = Decimal
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "desk_rails"
AS_OF = date(2026, 9, 24)

LIMITS = rails.limits_from_table(
    {
        "max_loss_per_trade_usd": "500",
        "max_book_loss_usd": "5000",
        "max_per_underlying": 2,
        "max_net_beta_delta_usd_per_1pct_spy": "250",
        "max_admissions_per_session": 3,
        "max_roundtrip_cost_frac_of_max_loss": "0.15",
        "min_leg_open_interest": 100,
        "max_leg_spread_frac_of_mid": "0.10",
        "min_long_single_abs_delta": "0.30",
        "max_chain_age_sessions": 1,
        "max_book_short_vega_usd_per_volpt": "100",
    }
)


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture()
def legacy(tmp_path: Path) -> tuple[Path, Path]:
    plans = tmp_path / "plans"
    state = tmp_path / "state"
    shutil.copytree(FIX / "plans", plans)
    shutil.copytree(FIX / "state", state)
    return plans, state


def _by_id(view: BookView) -> dict[str, rails.BookPosition]:
    return {p.id: p for p in view.positions}


# ------------------------------------------------------------------ legacy


class TestLegacyBook:
    def test_live_snapshot_reads_two_open_nvda_spreads(self, legacy, tmp_path) -> None:
        plans, state = legacy
        before = _tree_digest(tmp_path)
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=state)
        assert view.problems == ()
        pos = _by_id(view)
        assert set(pos) == {
            "legacy:putspread-20260922/nvda-oct",
            "legacy:putspread-20260922/nvda-nov",
        }
        oct_, nov = (
            pos["legacy:putspread-20260922/nvda-oct"],
            pos["legacy:putspread-20260922/nvda-nov"],
        )
        assert (oct_.underlying, oct_.status, oct_.max_loss_usd) == ("NVDA", "open", D("105.00"))
        # 0.21 x 5 x 100 = 105; 1.24 x 3 x 100 = 372
        assert (nov.underlying, nov.status, nov.max_loss_usd) == ("NVDA", "open", D("372.00"))
        assert oct_.source == "legacy:putspread-20260922"
        # risk is the caller's to supply
        assert oct_.risk == PositionRisk()
        # read-only: nothing written, nothing created (no lock files either)
        assert _tree_digest(tmp_path) == before

    def test_positions_carry_their_structure_and_held_quantity(self, legacy) -> None:
        """The deal miner prices each position's greeks from its legs: the
        position names its structure (a legacy put spread as its
        debit_vertical spec) and the quantity at risk (the OPEN quantity
        for an open position, the full quantity for a working entry)."""
        plans, state = legacy
        pos = _by_id(desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=state))
        oct_ = pos["legacy:putspread-20260922/nvda-oct"]
        assert oct_.spec is not None and oct_.spec.kind == "debit_vertical"
        assert [(g.right, g.action, g.strike, g.expiry) for g in oct_.spec.legs] == [
            ("P", "BUY", D("185.0"), date(2026, 10, 16)),
            ("P", "SELL", D("150.0"), date(2026, 10, 16)),
        ]
        assert oct_.quantity == 5 and pos["legacy:putspread-20260922/nvda-nov"].quantity == 3
        working = _by_id(
            desk_book.load_book(as_of=date(2026, 9, 18), plans_root=plans, state_root=state)
        )["legacy:putspread-20260918/qqq-nov"]
        assert working.status == "working" and working.quantity == 4
        assert working.spec is not None and working.spec.underlying == "QQQ"

    def test_superseded_planned_book_is_dormant_once_its_entry_date_passed(self, legacy) -> None:
        plans, state = legacy
        # on the 09-18 entry date its planned structures were live entries
        view = desk_book.load_book(as_of=date(2026, 9, 18), plans_root=plans, state_root=state)
        ids = {p.id for p in view.positions if p.source == "legacy:putspread-20260918"}
        assert ids == {
            "legacy:putspread-20260918/nvda-oct",
            "legacy:putspread-20260918/qqq-nov",
            "legacy:putspread-20260918/nvda-nov",
        }
        pos = _by_id(view)
        # working entries at their caps: 0.50 x 5 x 100, 2.40 x 4 x 100, 2.10 x 3 x 100
        assert pos["legacy:putspread-20260918/nvda-oct"].max_loss_usd == D("250.00")
        assert pos["legacy:putspread-20260918/qqq-nov"].max_loss_usd == D("960.00")
        assert pos["legacy:putspread-20260918/nvda-nov"].max_loss_usd == D("630.00")
        assert {p.status for p in view.positions if p.source == "legacy:putspread-20260918"} == {
            "working"
        }

    def test_nvda_is_blocked_account_wide_and_qqq_is_not(self, legacy, static_calendar) -> None:
        plans, state = legacy
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=state)
        flat = PositionRisk(
            delta_shares=D("-20"), vega_usd_per_volpt=D("3"), spot=D("180"), beta=D("1.8")
        )
        view = view.with_risk({p.id: flat for p in view.positions})
        ctx = RailContext(
            now=datetime(2026, 9, 24, 10, 0, tzinfo=ET),
            session=AS_OF,
            calendar=static_calendar,
            account_mode="paper",
            admissions_this_session=0,
        )
        nvda = _put_debit_spread("NVDA", "180", "170")
        report = rails.check(nvda, view, ctx, LIMITS)
        per = next(r for r in report.results if r.rule == "per_underlying")
        assert per.status == FAIL
        assert "legacy:putspread-20260922/nvda-oct" in per.detail
        assert "legacy:putspread-20260922/nvda-nov" in per.detail
        assert report.ok is False
        # the book max loss counted both: 105 + 372 + 300 = 777 <= 5000
        assert [r.status for r in report.results if r.rule == "max_book_loss"] == [PASS]
        assert "$777.00" in next(r.detail for r in report.results if r.rule == "max_book_loss")

        qqq = _put_debit_spread("QQQ", "600", "590")
        report = rails.check(qqq, view, ctx, LIMITS)
        assert [r.status for r in report.results if r.rule == "per_underlying"] == [PASS]
        assert report.ok is True

    def test_without_greeks_the_delta_rule_fails_closed(self, legacy, static_calendar) -> None:
        plans, state = legacy
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=state)
        ctx = RailContext(
            now=datetime(2026, 9, 24, 10, 0, tzinfo=ET),
            session=AS_OF,
            calendar=static_calendar,
            account_mode="paper",
            admissions_this_session=0,
        )
        report = rails.check(_put_debit_spread("QQQ", "600", "590"), view, ctx, LIMITS)
        assert [r.status for r in report.results if r.rule == "net_beta_delta"] == [NOT_EVALUABLE]


def _put_debit_spread(sym: str, long_k: str, short_k: str) -> Candidate:
    """1 lot at a 3.00 cap: max loss 3.00 x 100 = $300 (width 10)."""
    exp = date(2026, 12, 18)
    return Candidate(
        id=f"deal-{sym}",
        underlying=sym,
        kind="debit_vertical",
        legs=(
            CandidateLeg("P", "BUY", D(long_k), exp, D("6.00"), D("6.10"), 5000, D("-0.40")),
            CandidateLeg("P", "SELL", D(short_k), exp, D("3.00"), D("3.05"), 5000, D("-0.25")),
        ),
        quantity=1,
        max_loss_usd=D("300"),
        entry_price=D("3.00"),
        planned_exit=date(2026, 10, 22),
        risk=PositionRisk(
            delta_shares=D("-15"), vega_usd_per_volpt=D("2"), spot=D("600"), beta=D("1.1")
        ),
        chain_session=date(2026, 9, 23),
        chain_as_of=datetime(2026, 9, 24, 6, 40, tzinfo=ET),
        earnings=(),
        ex_dividends=(),
        news_veto=None,
    )


# ------------------------------------------------------- status mapping


PLAN = """
id = "{pid}"
account_mode = "paper"
total_debit_cap = 5000.00
entry_window_start = "09:45"
entry_window_end = "12:00"

[[structures]]
id = "a"
underlying = "SPY"
entry_date = {entry}
expiry = 2026-12-18
long_strike = 600.0
short_strike = 590.0
quantity = 4
limit_cap = 2.50
exit_deadline = 2026-12-04
"""


def _state(**over) -> dict[str, object]:
    base: dict[str, object] = {
        "status": "planned",
        "entry_order": None,
        "entry_fill": None,
        "filled_qty": 0,
        "entry_cycles": 0,
        "exit_order": None,
        "exit_fill": None,
        "exit_filled_qty": 0,
        "exit_cycles": 0,
        "exit_reason": None,
        "close_reason": None,
        "touch_ts": None,
        "updated_at": None,
    }
    base.update(over)
    return base


def _legacy_one(
    tmp_path: Path, *, entry: str, state: dict[str, object] | None
) -> tuple[Path, Path]:
    plans = tmp_path / "plans"
    root = tmp_path / "state"
    plans.mkdir()
    (plans / "p.toml").write_text(PLAN.format(pid="p1", entry=entry))
    if state is not None:
        (root / "p1").mkdir(parents=True)
        (root / "p1" / "book.json").write_text(
            json.dumps({"heartbeat": None, "structures": {"a": state}})
        )
    else:
        root.mkdir()
    return plans, root


class TestLegacyStatusMapping:
    # the spread's cap: 2.50 x 4 x 100 = $1000
    @pytest.mark.parametrize(
        ("entry", "state", "want"),
        [
            ("2026-09-24", _state(), ("working", D("1000.00"))),  # entry today
            ("2026-09-25", _state(), ("working", D("1000.00"))),  # entry ahead
            ("2026-09-23", _state(), None),  # entry date passed: the engine aborts it
            (
                "2026-09-23",
                _state(status="enter_working", entry_order="7"),
                ("working", D("1000.00")),
            ),
            (
                "2026-09-22",
                _state(status="open", entry_fill="1.10", filled_qty=4),
                ("open", D("440.00")),
            ),
            # partial exit: 0.30 x (4 - 1) x 100 = 90
            (
                "2026-09-22",
                _state(status="exit_working", entry_fill="0.30", filled_qty=4, exit_filled_qty=1),
                ("open", D("90.00")),
            ),
            # the smallest positions: 1 open (1.10 x 1 x 100), 1 exiting (0.30 x 1 x 100)
            (
                "2026-09-22",
                _state(status="open", entry_fill="1.10", filled_qty=1),
                ("open", D("110.00")),
            ),
            (
                "2026-09-22",
                _state(status="exit_working", entry_fill="0.30", filled_qty=1),
                ("open", D("30.00")),
            ),
            # no fill price on an open position: counted at the cap
            ("2026-09-22", _state(status="open", filled_qty=4), ("open", D("1000.00"))),
            ("2026-09-22", _state(status="closed", close_reason="exit: touch"), None),
        ],
    )
    def test_mapping(self, entry, state, want, tmp_path) -> None:
        plans, root = _legacy_one(tmp_path, entry=entry, state=state)
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=root)
        assert view.problems == ()
        got = [(p.status, p.max_loss_usd) for p in view.positions]
        assert got == ([] if want is None else [want])

    def test_plan_without_state_counts_as_planned(self, tmp_path) -> None:
        plans, root = _legacy_one(tmp_path, entry="2026-09-24", state=None)
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=root)
        assert [(p.id, p.status) for p in view.positions] == [("legacy:p1/a", "working")]


class TestLegacyFailsClosed:
    def test_unreadable_book_is_a_problem(self, legacy) -> None:
        plans, state = legacy
        (state / "putspread-20260922" / "book.json").write_text("{not json")
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=state)
        assert any("putspread-20260922" in p for p in view.problems)

    def test_invalid_plan_is_a_problem(self, legacy) -> None:
        plans, state = legacy
        (plans / "broken.toml").write_text('id = "x"\naccount_mode = "paper"\n')
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=state)
        assert any("broken.toml" in p for p in view.problems)

    def test_missing_plans_dir_is_a_problem(self, tmp_path) -> None:
        view = desk_book.load_book(as_of=AS_OF, plans_root=tmp_path / "nope", state_root=tmp_path)
        assert view.problems

    def test_book_structure_unknown_to_its_plan_is_a_problem(self, tmp_path) -> None:
        plans, root = _legacy_one(tmp_path, entry="2026-09-22", state=_state(status="closed"))
        doc = json.loads((root / "p1" / "book.json").read_text())
        doc["structures"]["ghost"] = _state(status="open", entry_fill="1.00", filled_qty=1)
        (root / "p1" / "book.json").write_text(json.dumps(doc))
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=root)
        assert any("ghost" in p for p in view.problems)

    def test_closed_unknown_structure_is_harmless(self, tmp_path) -> None:
        plans, root = _legacy_one(tmp_path, entry="2026-09-22", state=_state(status="closed"))
        doc = json.loads((root / "p1" / "book.json").read_text())
        doc["structures"]["ghost"] = _state(status="closed")
        (root / "p1" / "book.json").write_text(json.dumps(doc))
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=root)
        assert view.problems == ()
        assert view.positions == ()

    # -- P1-1: a book whose plan TOML is gone still counts ------------------

    def test_orphan_book_with_open_positions_is_a_problem(self, legacy) -> None:
        plans, state = legacy
        (plans / "2026-09-22.toml").unlink()  # archived plan, live NVDA pair
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=state)
        assert any("putspread-20260922" in p and "no plan" in p for p in view.problems)

    def test_orphan_book_that_never_entered_is_still_unresolved(self, legacy) -> None:
        plans, state = legacy
        (plans / "2026-09-18.toml").unlink()  # its structures are PLANNED, not CLOSED
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=state)
        assert any("putspread-20260918" in p for p in view.problems)

    def test_orphan_book_all_closed_is_harmless(self, legacy) -> None:
        plans, state = legacy
        (state / "old-plan").mkdir()
        (state / "old-plan" / "book.json").write_text(
            json.dumps({"heartbeat": None, "structures": {"x": _state(status="closed")}})
        )
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=state)
        assert view.problems == ()
        assert len(view.positions) == 2

    def test_unreadable_orphan_book_is_a_problem(self, legacy) -> None:
        plans, state = legacy
        (state / "old-plan").mkdir()
        (state / "old-plan" / "book.json").write_text("[1, 2")
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=state)
        assert any("old-plan" in p for p in view.problems)

    def test_state_dir_without_a_book_is_ignored(self, legacy) -> None:
        plans, state = legacy
        (state / "exit-watch").mkdir()
        (state / "exit-watch" / "monitor.lock").write_text("")
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=state)
        assert view.problems == ()

    def test_duplicate_plan_ids_are_a_problem(self, legacy) -> None:
        plans, state = legacy
        shutil.copy(plans / "2026-09-22.toml", plans / "copy.toml")
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=state)
        assert any("putspread-20260922" in p and "duplicate" in p for p in view.problems)

    # -- P1-2: seeded PLANNED is not evidence -------------------------------

    @pytest.mark.parametrize(
        "doc",
        [
            {},  # an emptied book
            {"heartbeat": None},
            {"heartbeat": None, "structures": {}},
        ],
    )
    def test_book_without_the_structure_after_its_entry_date(self, doc, tmp_path) -> None:
        plans, root = _legacy_one(tmp_path, entry="2026-09-22", state=_state())
        (root / "p1" / "book.json").write_text(json.dumps(doc))
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=root)
        assert any("legacy:p1/a" in p and "unresolved" in p for p in view.problems)

    def test_missing_book_file_after_the_entry_date(self, tmp_path) -> None:
        plans, root = _legacy_one(tmp_path, entry="2026-09-22", state=None)
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=root)
        assert any("legacy:p1/a" in p and "unresolved" in p for p in view.problems)

    def test_no_state_yet_before_the_entry_date_is_a_working_entry(self, tmp_path) -> None:
        plans, root = _legacy_one(tmp_path, entry="2026-09-24", state=_state())
        (root / "p1" / "book.json").write_text("{}")
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=root)
        assert view.problems == ()
        assert [(p.status, p.max_loss_usd) for p in view.positions] == [("working", D("1000.00"))]

    # -- P1-3: fills, quantities and states must be consistent --------------

    @pytest.mark.parametrize(
        "state",
        [
            _state(status="open", entry_fill="-0.10", filled_qty=4),  # a negative debit
            _state(status="open", entry_fill="NaN", filled_qty=4),
            _state(status="open", entry_fill="0", filled_qty=4),  # risks nothing: impossible
            _state(status="open", entry_fill="1.10", filled_qty=0),  # open with nothing filled
            _state(status="exit_working", entry_fill="1.10", filled_qty=2, exit_filled_qty=3),
            _state(status="open", entry_fill="1.10", filled_qty=5),  # more than the plan's 4
            _state(status="enter_working", entry_order="7", filled_qty=5),
            _state(status="enter_working", entry_order="7", entry_fill="0", filled_qty=1),
            _state(status="open", entry_fill="1.10", filled_qty=-1),
        ],
    )
    def test_inconsistent_state_is_a_problem(self, state, tmp_path) -> None:
        plans, root = _legacy_one(tmp_path, entry="2026-09-22", state=state)
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=root)
        assert any("legacy:p1/a" in p for p in view.problems)
        assert view.positions == ()

    def test_a_problem_makes_the_book_rules_not_evaluable(self, legacy, static_calendar) -> None:
        plans, state = legacy
        (state / "putspread-20260922" / "book.json").write_text("{not json")
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=state)
        ctx = RailContext(
            now=datetime(2026, 9, 24, 10, 0, tzinfo=ET),
            session=AS_OF,
            calendar=static_calendar,
            account_mode="paper",
            admissions_this_session=0,
        )
        report = rails.check(_put_debit_spread("QQQ", "600", "590"), view, ctx, LIMITS)
        statuses = {r.rule: r.status for r in report.results}
        for rule in ("max_book_loss", "per_underlying", "net_beta_delta", "book_short_vega"):
            assert statuses[rule] == NOT_EVALUABLE


# ------------------------------------------------------------------ desk


def _spec(sid: str = "d1", underlying: str = "XLF") -> LegStructure:
    """XLF 48/45 put credit spread, 2 lots at a 1.00 floor: max loss at the
    floor (3 - 1.00) x 100 x 2 = $400."""
    exp = date(2026, 11, 20)
    return LegStructure(
        id=sid,
        underlying=underlying,
        kind="credit_vertical",
        legs=(
            Leg(right="P", action="SELL", strike=D("48"), expiry=exp),
            Leg(right="P", action="BUY", strike=D("45"), expiry=exp),
        ),
        quantity=2,
        entry_date=date(2026, 9, 24),
        exit_deadline=date(2026, 10, 22),
        limit=D("1.00"),
        exits=ExitRules(touch=False, breach=True),
    )


class TestDeskSpecs:
    def _write(self, d: Path, spec: LegStructure) -> None:
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{spec.id}.json").write_text(spec.model_dump_json())

    def _empty_legacy(self, tmp_path: Path) -> tuple[Path, Path]:
        plans = tmp_path / "plans"
        plans.mkdir()
        return plans, tmp_path / "state"

    def test_admitted_spec_without_state_is_working_at_its_cap(self, tmp_path) -> None:
        specs = tmp_path / "desk" / "specs"
        self._write(specs, _spec())
        plans, root = self._empty_legacy(tmp_path)
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=root, desk_specs=specs)
        assert view.problems == ()
        [p] = view.positions
        assert (p.id, p.source, p.underlying, p.status, p.max_loss_usd) == (
            "desk:d1",
            "desk",
            "XLF",
            "working",
            D("400.00"),
        )
        # the spec itself rides along (the miner prices its greeks, and a
        # deal_id names the playbook row for the row's max_open)
        assert p.spec == _spec() and p.quantity == 2

    def test_open_credit_spec_risks_width_minus_its_credit(self, tmp_path) -> None:
        specs = tmp_path / "desk" / "specs"
        self._write(specs, _spec())
        book = tmp_path / "desk" / "book.json"
        book.write_text(
            json.dumps(
                {
                    "heartbeat": None,
                    "structures": {"d1": _state(status="open", entry_fill="1.10", filled_qty=2)},
                }
            )
        )
        plans, root = self._empty_legacy(tmp_path)
        view = desk_book.load_book(
            as_of=AS_OF, plans_root=plans, state_root=root, desk_specs=specs, desk_book=book
        )
        # (3 - 1.10) x 100 x 2 = 380
        assert [(p.status, p.max_loss_usd) for p in view.positions] == [("open", D("380.00"))]

    @pytest.mark.parametrize("fill", ["3.00", "3.50", "-1"])
    def test_credit_fill_that_leaves_no_loss_is_a_problem(self, fill, tmp_path) -> None:
        # width 3: a 3.00 credit risks 0, a 3.50 one "risks" -$100 per package
        specs = tmp_path / "desk" / "specs"
        self._write(specs, _spec())
        book = tmp_path / "desk" / "book.json"
        book.write_text(
            json.dumps(
                {
                    "heartbeat": None,
                    "structures": {"d1": _state(status="open", entry_fill=fill, filled_qty=2)},
                }
            )
        )
        plans, root = self._empty_legacy(tmp_path)
        view = desk_book.load_book(
            as_of=AS_OF, plans_root=plans, state_root=root, desk_specs=specs, desk_book=book
        )
        assert any("desk:d1" in p for p in view.problems)
        assert view.positions == ()

    def test_desk_run_dir_under_the_legacy_state_root(self, tmp_path) -> None:
        # the Wave 3 runtime's run dir is ~/.local/state/trex/desk-paper/: passed as
        # the desk book it is the desk's; not passed, it is an orphan book (fail closed)
        plans, root = self._empty_legacy(tmp_path)
        specs = tmp_path / "desk-specs"
        self._write(specs, _spec())
        run = root / "desk-paper"
        run.mkdir(parents=True)
        (run / "book.json").write_text(
            json.dumps(
                {
                    "heartbeat": None,
                    "structures": {"d1": _state(status="open", entry_fill="1.10", filled_qty=2)},
                }
            )
        )
        view = desk_book.load_book(
            as_of=AS_OF,
            plans_root=plans,
            state_root=root,
            desk_specs=specs,
            desk_book=run / "book.json",
        )
        assert view.problems == ()
        assert [p.id for p in view.positions] == ["desk:d1"]
        unwired = desk_book.load_book(
            as_of=AS_OF, plans_root=plans, state_root=root, desk_specs=specs
        )
        assert any("desk-paper" in p for p in unwired.problems)

    def test_closed_spec_is_not_exposure(self, tmp_path) -> None:
        specs = tmp_path / "desk" / "specs"
        self._write(specs, _spec())
        book = tmp_path / "desk" / "book.json"
        book.write_text(
            json.dumps({"heartbeat": None, "structures": {"d1": _state(status="closed")}})
        )
        plans, root = self._empty_legacy(tmp_path)
        view = desk_book.load_book(
            as_of=AS_OF, plans_root=plans, state_root=root, desk_specs=specs, desk_book=book
        )
        assert view.positions == () and view.problems == ()

    def test_unparseable_spec_is_a_problem(self, tmp_path) -> None:
        specs = tmp_path / "desk" / "specs"
        specs.mkdir(parents=True)
        (specs / "bad.json").write_text('{"id": "bad"}')
        plans, root = self._empty_legacy(tmp_path)
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=root, desk_specs=specs)
        assert any("bad.json" in p for p in view.problems)

    def test_absent_spec_dir_is_an_empty_desk(self, tmp_path) -> None:
        plans, root = self._empty_legacy(tmp_path)
        view = desk_book.load_book(
            as_of=AS_OF, plans_root=plans, state_root=root, desk_specs=tmp_path / "none"
        )
        assert view.positions == () and view.problems == ()

    def test_desk_state_for_an_unknown_spec_is_a_problem(self, tmp_path) -> None:
        specs = tmp_path / "desk" / "specs"
        specs.mkdir(parents=True)
        book = tmp_path / "desk" / "book.json"
        book.write_text(
            json.dumps(
                {"heartbeat": None, "structures": {"zz": _state(status="open", filled_qty=1)}}
            )
        )
        plans, root = self._empty_legacy(tmp_path)
        view = desk_book.load_book(
            as_of=AS_OF, plans_root=plans, state_root=root, desk_specs=specs, desk_book=book
        )
        assert any("zz" in p for p in view.problems)

    def test_legacy_and_desk_count_together(self, legacy, tmp_path) -> None:
        plans, state = legacy
        specs = tmp_path / "desk" / "specs"
        self._write(specs, _spec("d-nvda", "NVDA"))
        view = desk_book.load_book(
            as_of=AS_OF, plans_root=plans, state_root=state, desk_specs=specs
        )
        assert sorted(p.id for p in view.positions if p.underlying == "NVDA") == [
            "desk:d-nvda",
            "legacy:putspread-20260922/nvda-nov",
            "legacy:putspread-20260922/nvda-oct",
        ]


class TestWithRisk:
    def test_attaches_by_id_and_leaves_the_rest_empty(self, legacy) -> None:
        plans, state = legacy
        view = desk_book.load_book(as_of=AS_OF, plans_root=plans, state_root=state)
        r = PositionRisk(delta_shares=D("1"), vega_usd_per_volpt=D("1"), spot=D("1"), beta=D("1"))
        out = _by_id(view.with_risk({"legacy:putspread-20260922/nvda-oct": r}))
        assert out["legacy:putspread-20260922/nvda-oct"].risk == r
        assert out["legacy:putspread-20260922/nvda-nov"].risk == PositionRisk()


class TestDefaults:
    def test_default_roots_follow_the_environment(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("TREX_STATE", str(tmp_path / "s"))
        monkeypatch.setenv("DESK_REPO_ROOT", str(tmp_path / "repo"))
        assert desk_book.default_state_root() == tmp_path / "s"
        assert desk_book.default_plans_root() == tmp_path / "repo" / "plans"
        monkeypatch.delenv("TREX_STATE")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        assert desk_book.default_state_root() == tmp_path / "home" / ".local" / "state" / "trex"
