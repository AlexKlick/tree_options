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
            # partial exit: 0.30 x (5 - 2) x 100 = 90 (the filled 5 exceed the plan's 4 on purpose:
            # the book's own quantities rule)
            (
                "2026-09-22",
                _state(status="exit_working", entry_fill="0.30", filled_qty=5, exit_filled_qty=2),
                ("open", D("90.00")),
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
