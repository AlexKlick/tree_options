"""Costed, next-session machinery tests; fixture outcomes are not market evidence."""

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

import pytest

from tree_options.research.quant import FrozenUniverse, QuantSnapshot, register_version
from tree_options.research.quant_backtest import ReplayPeriod, evaluate_period, evaluate_periods
from tree_options.research.runstate.store import open_runstate_store
from tree_options.strategy_lab.contracts import Observation
from tree_options.time.calendar import StaticSessionCalendar
from tree_options.time.sessions import early_close_instant, session_close_instant


@pytest.fixture
def calendar(tmp_path):
    path = tmp_path / "calendar.json"
    path.write_text(
        json.dumps(
            {
                "calendar": "synthetic-test",
                "timezone": "America/New_York",
                "open": "09:30",
                "close": "16:00",
                "sessions": [
                    "2025-11-28",
                    "2025-12-31",
                    "2026-10-30",
                    "2026-11-25",
                    "2026-11-27",
                    "2026-11-30",
                    "2026-12-01",
                ],
                "early_close_sessions": ["2025-11-28", "2026-11-27"],
            }
        )
    )
    checksum = tmp_path / "calendar.sha256"
    checksum.write_text(hashlib.sha256(path.read_bytes()).hexdigest())
    return StaticSessionCalendar(path, checksum)


def period(calendar, *, observations=True, decision=date(2026, 11, 25)):
    cutoff = calendar.session_close(decision)
    rows = []
    if observations:
        for symbol, values in (("A", ("100", "90", "9999")), ("B", ("100", "120", "1"))):
            for day, value in zip(
                (date(2025, 11, 28), date(2026, 10, 30), decision), values, strict=True
            ):
                at = calendar.session_close(day)
                rows.append(
                    Observation(
                        symbol, at, at, {"close": Decimal(value)}, "fixture", f"{symbol}-{day}"
                    )
                )
    snapshot = QuantSnapshot(
        FrozenUniverse(decision, ("A", "B"), "synthetic-test", "a" * 64), cutoff, tuple(rows)
    )
    execution_session = calendar.nth_after(decision, 1)
    return ReplayPeriod(
        snapshot,
        calendar.session_open(execution_session),
        calendar.session_close(execution_session),
        {"A": Decimal("100"), "B": Decimal("100")},
        {"A": Decimal("90"), "B": Decimal("120")},
    )


def version(store, strategy="equal_weight_us_equities", **parameters):
    return register_version(
        store, strategy, code_sha="a" * 40, lock_sha="b" * 64, parameters=parameters
    )


def test_costed_integer_roundtrip_uses_existing_funded_ledger(tmp_path, calendar):
    with open_runstate_store(tmp_path / "store") as store:
        result = evaluate_period(store, version(store), period(calendar), calendar)
        executions = result["executions"]
        assert [item["signed_quantity"] for item in executions] == [49, 49, -49, -49]
        assert result["evidence_kind"] == "BACKTEST"
        assert result["exact_external_economics"] is False
        assert result["execution_authorized"] is False
        assert result["accounting_model"] == "existing_funded_account_fifo/1"
        assert Decimal(result["fees"]) > 0
        assert Decimal(result["nav"]) == Decimal("10000") + sum(
            -Decimal(item["price"]) * item["signed_quantity"] - Decimal(item["fees"])
            for item in executions
        )
        assert result["positions"] == []
        assert Decimal(result["net_return"]) == (Decimal(result["nav"]) / Decimal("10000") - 1)
        assert result["drawdown_scope"] == "starting_capital_to_terminal_cash_only"
        assert store.get("quant_backtest_period", result["backtest_id"]) == result
        assert store.verify()["ok"]


def test_equal_control_and_momentum_candidate_select_different_targets(tmp_path, calendar):
    with open_runstate_store(tmp_path / "store") as store:
        control = evaluate_period(store, version(store), period(calendar), calendar)
        candidate = evaluate_period(
            store, version(store, "momentum_12_1", top_n=1), period(calendar), calendar
        )
        assert {target["entity_id"] for target in control["research_run"]["targets"]} == {"A", "B"}
        assert candidate["research_run"]["targets"] == [{"entity_id": "B", "weight": "1"}]
        assert Decimal(candidate["net_return"]) > Decimal(control["net_return"])


def test_future_execution_price_poisoning_cannot_change_scores(tmp_path, calendar):
    with open_runstate_store(tmp_path / "store") as store:
        v = version(store, "momentum_12_1", top_n=1)
        p = period(calendar)
        original = evaluate_period(store, v, p, calendar)
        changed = evaluate_period(
            store,
            v,
            replace(
                p,
                opens={"A": Decimal("9000"), "B": Decimal("100")},
                closes={"A": Decimal("1"), "B": Decimal("99999")},
            ),
            calendar,
        )
        assert changed["research_run"] == original["research_run"]
        assert changed["backtest_id"] != original["backtest_id"]
        assert changed["net_return"] != original["net_return"]


def test_more_slippage_reduces_modeled_return(tmp_path, calendar):
    with open_runstate_store(tmp_path / "store") as store:
        v = version(store)
        low = evaluate_period(store, v, period(calendar), calendar, slippage_bps=Decimal("0"))
        high = evaluate_period(store, v, period(calendar), calendar, slippage_bps=Decimal("50"))
        assert Decimal(high["net_return"]) < Decimal(low["net_return"])
        assert high["backtest_id"] != low["backtest_id"]


def test_loss_and_turnover_are_measured_on_declared_cash_reset_basis(tmp_path, calendar):
    with open_runstate_store(tmp_path / "store") as store:
        result = evaluate_period(store, version(store, top_n=1), period(calendar), calendar)
        assert result["research_run"]["targets"] == [{"entity_id": "A", "weight": "1"}]
        assert Decimal(result["nav"]) == Decimal("8981.79")
        assert Decimal(result["fees"]) == Decimal("9.40")
        assert Decimal(result["max_drawdown"]) == Decimal("0.101821")
        assert Decimal(result["turnover"]) == Decimal("1.881099")


def test_price_maps_detached_and_permutation_invariant(tmp_path, calendar):
    with open_runstate_store(tmp_path / "store") as store:
        p = period(calendar)
        source = {"B": Decimal("100"), "A": Decimal("100")}
        copied = replace(p, opens=source, closes=dict(reversed(list(p.closes.items()))))
        identity = copied.identity
        source["A"] = Decimal("200")
        assert copied.identity == identity == p.identity
        v = version(store)
        assert evaluate_period(store, v, copied, calendar) == evaluate_period(store, v, p, calendar)
        with pytest.raises(TypeError):
            copied.opens["A"] = Decimal("200")


@pytest.mark.parametrize("which", ["execution_at", "mark_at"])
def test_naive_times_refused(calendar, which):
    with pytest.raises(ValueError, match="timezone"):
        replace(period(calendar), **{which: datetime(2026, 11, 27)})


@pytest.mark.parametrize("which", ["opens", "closes"])
@pytest.mark.parametrize(
    "bad", [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), "100", 100.0]
)
def test_invalid_prices_refused(calendar, which, bad):
    with pytest.raises(ValueError, match="price"):
        replace(period(calendar), **{which: {"A": bad}})


def test_out_of_universe_execution_prices_refused(calendar):
    with pytest.raises(ValueError, match="frozen-universe"):
        replace(period(calendar), opens={"TODAYS_ADDITION": Decimal("100")})


def test_fill_price_rounding_cannot_exceed_existing_ledger_precision(tmp_path, calendar):
    with open_runstate_store(tmp_path / "store") as store:
        p = period(calendar)
        overflow = replace(p, opens={"A": Decimal("9999999999.996"), "B": Decimal("100")})
        with pytest.raises(ValueError, match="ledger precision"):
            evaluate_period(store, version(store), overflow, calendar, slippage_bps=Decimal("0"))


def test_future_observation_cannot_be_used_as_score_input(calendar):
    p = period(calendar)
    future = Observation(
        "A", p.mark_at, p.mark_at, {"close": Decimal("900000")}, "fixture", "future"
    )
    with pytest.raises(ValueError, match="cutoff"):
        QuantSnapshot(p.snapshot.universe, p.snapshot.cutoff, (*p.snapshot.observations, future))


@pytest.mark.parametrize("which", ["opens", "closes"])
def test_missing_selected_prices_refused(tmp_path, calendar, which):
    with open_runstate_store(tmp_path / "store") as store:
        with pytest.raises(ValueError, match="missing selected"):
            evaluate_period(
                store,
                version(store),
                replace(period(calendar), **{which: {"A": Decimal("100")}}),
                calendar,
            )
        assert store.all("quant_backtest_period") == ()


def test_exact_next_open_and_early_close_required(tmp_path, calendar):
    with open_runstate_store(tmp_path / "store") as store:
        p = period(calendar)
        v = version(store)
        result = evaluate_period(store, v, p, calendar)
        assert result["mark_at"] == early_close_instant(date(2026, 11, 27)).isoformat()
        with pytest.raises(ValueError, match="next session open"):
            evaluate_period(
                store,
                v,
                replace(
                    p,
                    execution_at=calendar.session_open(date(2026, 11, 30)),
                    mark_at=calendar.session_close(date(2026, 11, 30)),
                ),
                calendar,
            )
        with pytest.raises(ValueError, match="session close"):
            evaluate_period(
                store, v, replace(p, mark_at=session_close_instant(date(2026, 11, 27))), calendar
            )
        with pytest.raises(ValueError, match="after decision"):
            replace(p, execution_at=p.snapshot.cutoff)


@pytest.mark.parametrize(
    "key,bad",
    [
        ("capital", Decimal("0")),
        ("capital", Decimal("NaN")),
        ("capital", Decimal("100.001")),
        ("slippage_bps", Decimal("-1")),
        ("slippage_bps", Decimal("10000")),
        ("slippage_bps", Decimal("Infinity")),
    ],
)
def test_cost_configuration_refused(tmp_path, calendar, key, bad):
    with open_runstate_store(tmp_path / "store") as store:
        with pytest.raises(ValueError, match=r"capital|slippage"):
            evaluate_period(store, version(store), period(calendar), calendar, **{key: bad})


def test_no_signal_and_gated_lane_emit_no_economics(tmp_path, calendar):
    with open_runstate_store(tmp_path / "store") as store:
        no_signal = evaluate_period(
            store, version(store), period(calendar, observations=False), calendar
        )
        assert no_signal["disposition"] == "NO_SIGNAL"
        assert no_signal["net_return"] is None and no_signal["nav"] is None
        assert no_signal["executions"] == []
        gated = evaluate_period(
            store, version(store, "robust_value_5metric"), period(calendar), calendar
        )
        assert gated["disposition"] == "DATA-GATED-NOT-RUN"
        assert gated["net_return"] is None and gated["executions"] == []


def test_unaffordable_targets_do_not_fabricate_return(tmp_path, calendar):
    with open_runstate_store(tmp_path / "store") as store:
        result = evaluate_period(
            store, version(store), period(calendar), calendar, capital=Decimal("1")
        )
        assert result["disposition"] == "NO_AFFORDABLE_POSITION"
        assert result["nav"] is None and result["net_return"] is None
        assert result["executions"] == []


def test_period_aggregation_is_explicit_cash_reset_not_compounding(tmp_path, calendar):
    with open_runstate_store(tmp_path / "store") as store:
        first = period(calendar)
        second = period(calendar, decision=date(2026, 11, 30))
        result = evaluate_periods(store, version(store), (first, second), calendar)
        assert result["capital_policy"] == "independent_equal_capital_roundtrips"
        assert result["turnover_aggregation"] == "mean_per_period"
        assert result["fees_aggregation"] == "sum"
        assert result["period_count"] == 2
        assert result["scored_period_count"] == 2
        assert (
            Decimal(result["mean_net_return"])
            == sum(Decimal(row["net_return"]) for row in result["periods"]) / 2
        )
        assert result["compound_nav"] is None
        assert result["max_drawdown"] == str(
            max(Decimal(row["max_drawdown"]) for row in result["periods"])
        )


def test_duplicate_or_out_of_order_periods_refused(tmp_path, calendar):
    with open_runstate_store(tmp_path / "store") as store:
        p = period(calendar)
        v = version(store)
        with pytest.raises(ValueError, match="strictly increasing"):
            evaluate_periods(store, v, (p, p), calendar)
        with pytest.raises(ValueError, match="nonempty"):
            evaluate_periods(store, v, (), calendar)


def test_more_than_sixty_fills_preserve_funded_ledger_ordering(tmp_path, calendar):
    p = period(calendar)
    symbols = tuple(f"S{index:02}" for index in range(35))
    observations = tuple(
        Observation(
            symbol,
            p.snapshot.cutoff,
            p.snapshot.cutoff,
            {"close": Decimal("100")},
            "fixture",
            symbol,
        )
        for symbol in symbols
    )
    snap = QuantSnapshot(
        FrozenUniverse(p.snapshot.universe.as_of, symbols, "fixture", "a" * 64),
        p.snapshot.cutoff,
        observations,
    )
    prices = dict.fromkeys(symbols, Decimal("100"))
    with open_runstate_store(tmp_path / "store") as store:
        result = evaluate_period(
            store,
            version(store),
            ReplayPeriod(snap, p.execution_at, p.mark_at, prices, prices),
            calendar,
        )
        assert result["disposition"] == "SCORED"
        assert len(result["executions"]) == 70
        assert result["positions"] == []
        assert Decimal(result["nav"]) < Decimal("10000")


def test_incomplete_period_cannot_rank_on_successful_subset(tmp_path, calendar):
    with open_runstate_store(tmp_path / "store") as store:
        first = period(calendar)
        second = period(calendar, observations=False, decision=date(2026, 11, 30))
        result = evaluate_periods(store, version(store), (first, second), calendar)
        assert result["disposition"] == "INCOMPLETE"
        assert result["scored_period_count"] == 1
        assert result["mean_net_return"] is None
        assert result["max_drawdown"] is None
        assert result["turnover"] is None
