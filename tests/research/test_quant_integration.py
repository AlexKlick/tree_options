from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tree_options.research.quant import (
    FrozenUniverse,
    QuantSnapshot,
    register_version,
    run_strategy,
)
from tree_options.research.runstate.store import open_runstate_store
from tree_options.strategy_lab.contracts import Observation
from tree_options.time.calendar import StaticSessionCalendar
from tree_options.time.sessions import session_close_instant

NOW = datetime(2026, 9, 29, 20, tzinfo=UTC)


@pytest.fixture
def calendar(tmp_path):
    import hashlib
    import json

    sessions = (
        "2025-09-30",
        "2025-10-31",
        "2025-11-28",
        "2025-12-31",
        "2026-01-30",
        "2026-02-27",
        "2026-03-31",
        "2026-04-30",
        "2026-05-29",
        "2026-06-30",
        "2026-07-31",
        "2026-08-31",
        "2026-09-01",
        "2026-09-29",
        "2026-09-30",
    )
    path = tmp_path / "calendar.json"
    path.write_text(
        json.dumps(
            {
                "calendar": "quant-fixture",
                "timezone": "America/New_York",
                "open": "09:30",
                "close": "16:00",
                "sessions": sessions,
                "early_close_sessions": ["2025-11-28"],
            }
        )
    )
    checksum = tmp_path / "calendar.sha256"
    checksum.write_text(hashlib.sha256(path.read_bytes()).hexdigest())
    return StaticSessionCalendar(path, checksum)


def snapshot(members=("A", "B"), as_of=date(2026, 9, 29)):
    observations = []
    for symbol in members:
        for session, price in (
            (date(2025, 9, 30), "100"),
            (date(2026, 8, 31), "110"),
            (date(2026, 9, 29), "9999"),
        ):
            at = session_close_instant(session)
            observations.append(
                Observation(
                    symbol, at, at, {"close": Decimal(price)}, "fixture", f"{symbol}-{session}"
                )
            )
    return QuantSnapshot(
        FrozenUniverse(as_of, members, "historical-fixture", "a" * 64), NOW, tuple(observations)
    )


def test_manifest_refuses_today_universe_and_future_observation():
    with pytest.raises(ValueError, match="universe"):
        snapshot(as_of=date(2026, 9, 30))
    s = snapshot()
    future = Observation(
        "A",
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 10, 1, tzinfo=UTC),
        {"close": Decimal("1")},
        "fixture",
        "future",
    )
    with pytest.raises(ValueError, match="cutoff"):
        QuantSnapshot(s.universe, NOW, (*s.observations, future))


def test_momentum_skip_no_signal_and_replay_identity(tmp_path, calendar):
    with open_runstate_store(tmp_path) as store:
        version = register_version(
            store, "momentum_12_1", code_sha="b" * 40, lock_sha="c" * 64, parameters={"top_n": 1}
        )
        result = run_strategy(store, version, snapshot(), calendar=calendar)
        assert result["scores"][0]["components"]["return_12_1"] == "0.1"
        assert result["targets"] == [{"entity_id": "A", "weight": "1"}]
        assert run_strategy(store, version, snapshot(), calendar=calendar) == result
        assert store.verify()["ok"]
        empty = QuantSnapshot(FrozenUniverse(NOW.date(), (), "fixture", "a" * 64), NOW, ())
        assert run_strategy(store, version, empty, calendar=calendar)["disposition"] == "NO_SIGNAL"
        assert result["evidence"]["exact_versions"]["strategy"] == version["version_id"]
        assert store.all("quant_snapshot") and store.all("quant_version")


def test_order_invariance_and_gated_value(tmp_path):
    with open_runstate_store(tmp_path) as store:
        version = register_version(
            store, "equal_weight_us_equities", code_sha="b" * 40, lock_sha="c" * 64
        )
        a = run_strategy(store, version, snapshot())
        s = snapshot()
        reverse = QuantSnapshot(
            FrozenUniverse(s.universe.as_of, ("B", "A"), "historical-fixture", "a" * 64),
            NOW,
            tuple(reversed(s.observations)),
        )
        assert run_strategy(store, version, reverse) == a
        assert sum(Decimal(row["weight"]) for row in a["targets"]) == 1
        value = register_version(
            store, "robust_value_5metric", code_sha="b" * 40, lock_sha="c" * 64
        )
        gated = run_strategy(store, value, s)
        assert gated["disposition"] == "DATA-GATED-NOT-RUN"
        assert gated["targets"] == []


def test_snapshot_detaches_mutable_inputs(tmp_path):
    values = {"close": Decimal("10")}
    at = datetime(2026, 9, 1, tzinfo=UTC)
    obs = Observation("A", at, at, values, "fixture", "a")
    s = QuantSnapshot(FrozenUniverse(NOW.date(), ("A",), "fixture", "a" * 64), NOW, (obs,))
    before = s.identity
    values["close"] = Decimal("1000")
    assert s.identity == before
    assert s.observations[0].values["close"] == Decimal("10")


def test_execution_attribution_requires_real_evidence():
    from tree_options.execution.lifecycle import ExecutionLifecycle
    from tree_options.execution.records import OrderIntent
    from tree_options.research.quant import execution_for_funded_replay

    order = OrderIntent(
        intent_id="q",
        contract_id="A",
        side="BUY",
        position_effect="OPEN_LONG",
        quantity=1,
        order_type="MARKET",
        intent_created_at=NOW,
        source="fixture",
        source_sequence_id="q",
    )
    with pytest.raises(ValueError, match="evidence"):
        execution_for_funded_replay(ExecutionLifecycle.start(order))


def test_cluster_lane_uses_frozen_features_and_exploratory_identity(tmp_path):
    with open_runstate_store(tmp_path) as store:
        members = ("A", "B", "C", "D")
        obs = tuple(
            Observation(
                k,
                NOW,
                NOW,
                {"rsi": Decimal(str(v)), "momentum": Decimal("0"), "factor_beta": Decimal("0")},
                "fixture",
                k,
            )
            for k, v in zip(members, (30, 45, 55, 70), strict=True)
        )
        s = QuantSnapshot(FrozenUniverse(NOW.date(), members, "fixture", "a" * 64), NOW, obs)
        version = register_version(
            store, "cluster_rsi_factor", code_sha="b" * 40, lock_sha="c" * 64
        )
        result = run_strategy(store, version, s)
        assert result["disposition"] == "SCORED"
        assert result["targets"] == [{"entity_id": "D", "weight": "1"}]
        assert result["evidence"]["diagnostics"]["registration"] == "exploratory"
        assert "D" not in result["exclusions"]
        assert "D" not in result["evidence"]["diagnostics"]["exclusions"]
        assert set(result["exclusions"]) == {"A", "B", "C"}


def test_partial_fill_cannot_bypass_attribution_evidence_gate():
    from tree_options.execution.lifecycle import ExecutionLifecycle
    from tree_options.execution.records import OrderIntent, PartialFill, SubmitAttempt
    from tree_options.research.quant import execution_for_funded_replay

    order = OrderIntent(
        intent_id="q",
        contract_id="A",
        side="BUY",
        position_effect="OPEN_LONG",
        quantity=2,
        order_type="MARKET",
        intent_created_at=NOW,
        source="fixture",
        source_sequence_id="q",
    )
    lifecycle = ExecutionLifecycle.start(order).apply(
        SubmitAttempt(
            record_id="s",
            intent_id="q",
            send_attempt_at=NOW,
            source="fixture",
            source_sequence_id="s",
        )
    )
    lifecycle = lifecycle.apply(
        PartialFill(
            record_id="f",
            intent_id="q",
            broker_order_id="b",
            fill_quantity=1,
            cumulative_quantity=1,
            unit_price=Decimal("50"),
            fees=Decimal("0"),
            exchange_event_at=NOW,
            locally_received_at=NOW,
            source="fixture",
            source_sequence_id="f",
            broker_sequence_id="f",
        )
    )
    with pytest.raises(ValueError, match="evidence"):
        execution_for_funded_replay(lifecycle)


def test_campaign_comparison_freezes_control_and_never_authorizes_effect(tmp_path, calendar):
    from tree_options.research.quant import campaign_proposal

    with open_runstate_store(tmp_path) as store:
        control = register_version(
            store, "equal_weight_us_equities", code_sha="b" * 40, lock_sha="c" * 64
        )
        candidate = register_version(store, "momentum_12_1", code_sha="b" * 40, lock_sha="c" * 64)
        a, b = (run_strategy(store, v, snapshot(), calendar=calendar) for v in (candidate, control))
        campaign = campaign_proposal(store, a["run_id"], b["run_id"])
        assert campaign["execution_authorized"] is False
        assert campaign["live_money"] is False
        assert campaign["common_snapshot"] == a["snapshot_id"] == b["snapshot_id"]
        assert campaign["max_orders"] == 1
        assert store.all("comparison_row") and store.all("quant_provenance")


def test_snapshot_nested_metadata_is_detached_frozen_and_canonically_serialized():
    metadata = {"provenance": {"receipts": [{"id": "original"}]}}
    at = session_close_instant(NOW.date())
    obs = Observation("A", at, at, {"close": Decimal("10")}, "fixture", "nested", metadata)
    s = QuantSnapshot(FrozenUniverse(NOW.date(), ("A",), "fixture", "a" * 64), NOW, (obs,))
    before = s.identity
    with pytest.raises(TypeError):
        obs.metadata["provenance"]["receipts"][0]["id"] = "changed"
    assert s.identity == before
    with pytest.raises(TypeError):
        s.observations[0].metadata["provenance"]["receipts"][0]["id"] = "changed"
    assert s.to_dict()["observations"][0]["metadata"] == metadata
    exported = s.to_dict()
    exported["observations"][0]["metadata"]["provenance"]["receipts"][0]["id"] = "changed"
    assert s.identity == before


def test_conflicting_same_event_closes_are_refused_even_with_different_availability():
    at = session_close_instant(date(2026, 8, 31))
    rows = (
        Observation("A", at, at, {"close": Decimal("10")}, "first", "first"),
        Observation("A", at, NOW, {"close": Decimal("20")}, "other", "other"),
    )
    with pytest.raises(ValueError, match="conflicting close"):
        QuantSnapshot(FrozenUniverse(NOW.date(), ("A",), "fixture", "a" * 64), NOW, rows)


def test_momentum_requires_exact_calendar_endpoints_and_explicit_calendar(tmp_path, calendar):
    with open_runstate_store(tmp_path / "runs") as store:
        version = register_version(store, "momentum_12_1", code_sha="b" * 40, lock_sha="c" * 64)
        with pytest.raises(ValueError, match="calendar"):
            run_strategy(store, version, snapshot())
        base = snapshot(members=("A",))
        august = next(o for o in base.observations if o.event_at.month == 8)
        early = session_close_instant(date(2026, 8, 3))
        rows = (
            *(o for o in base.observations if o is not august),
            Observation("A", early, early, {"close": Decimal("110")}, "fixture", "early-august"),
        )
        missing = QuantSnapshot(base.universe, NOW, rows)
        result = run_strategy(store, version, missing, calendar=calendar)
        assert result["disposition"] == "NO_SIGNAL"
        assert result["exclusions"] == {"A": "missing_12_1_endpoints"}
        assert result["targets"] == []
        complete = run_strategy(store, version, base, calendar=calendar)
        assert complete["scores"][0]["components"]["return_12_1"] == "0.1"
        assert complete["monthly_endpoints"]["2026-08"] == "2026-08-31"
        assert complete["evidence"]["exact_versions"]["calendar"] == complete["calendar_sha256"]


def test_hqm_uses_exact_session_close(tmp_path, calendar):
    sessions = (
        date(2025, 9, 30),
        date(2026, 3, 31),
        date(2026, 6, 30),
        date(2026, 8, 31),
        NOW.date(),
    )
    rows = tuple(
        Observation(
            "A",
            calendar.session_close(d),
            calendar.session_close(d),
            {"close": Decimal("10") + i},
            "fixture",
            str(d),
        )
        for i, d in enumerate(sessions)
    )
    s = QuantSnapshot(FrozenUniverse(NOW.date(), ("A",), "fixture", "a" * 64), NOW, rows)
    with open_runstate_store(tmp_path / "runs") as store:
        version = register_version(store, "hqm_1_3_6_12", code_sha="b" * 40, lock_sha="c" * 64)
        result = run_strategy(store, version, s, calendar=calendar)
        assert result["disposition"] == "SCORED"
        assert set(result["scores"][0]["components"]) == {"1m", "3m", "6m", "12m"}
        assert result["monthly_endpoints"]["2026-09"] == "2026-09-29"


def test_universe_session_date_uses_exchange_timezone_at_utc_midnight():
    cutoff = datetime(2026, 9, 30, 0, tzinfo=UTC)
    s = QuantSnapshot(FrozenUniverse(date(2026, 9, 29), (), "fixture", "a" * 64), cutoff, ())
    assert s.universe.as_of == date(2026, 9, 29)


def test_calendar_early_close_is_the_endpoint_not_normal_close(tmp_path, calendar):
    from tree_options.research.quant import _monthly_prices

    session = date(2025, 11, 28)
    cutoff = session_close_instant(date(2025, 12, 31))
    universe = FrozenUniverse(cutoff.date(), ("A",), "fixture", "a" * 64)
    early = calendar.session_close(session)
    normal = session_close_instant(session)
    for at, expected in ((early, {2025 * 12 + 11: Decimal("10")}), (normal, {})):
        obs = Observation("A", at, at, {"close": Decimal("10")}, "fixture", "close")
        s = QuantSnapshot(universe, cutoff, (obs,))
        assert (
            _monthly_prices(s, "A", endpoints={2025 * 12 + 11: session}, calendar=calendar)
            == expected
        )


def test_comparison_requires_common_calendar_identity(tmp_path, calendar):
    from tree_options.research.quant import compare_quant_runs

    with open_runstate_store(tmp_path / "runs") as store:
        candidate = register_version(store, "momentum_12_1", code_sha="b" * 40, lock_sha="c" * 64)
        control = register_version(
            store, "equal_weight_us_equities", code_sha="b" * 40, lock_sha="c" * 64
        )
        a = run_strategy(store, candidate, snapshot(), calendar=calendar)
        b = run_strategy(store, control, snapshot())
        with pytest.raises(ValueError, match="common frozen inputs"):
            compare_quant_runs(store, a["run_id"], b["run_id"])
