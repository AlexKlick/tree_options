"""Canary screening from a live paper session, and the whole supervised chain.

The collector drives the REAL ``IbkrTrex`` over ``SupervisedGateway``;
blockers are the pure ``review_canary_package`` screen's own reason strings,
so these tests pin which OBSERVATION produces which blocker. The last test
rehearses the complete supervised path on fakes: mandate, intent, screening,
permit bound to the screening digest, send through the IBKR adapter.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from tests.unit.trex_fakes import SupervisedGateway, position_row
from tree_options.action_graph.capital import CapitalProfile
from tree_options.execution import ExecutionState, OrderIntent
from tree_options.time.sessions import shift_instant
from tree_options.trex.ibkr import IbkrTrex
from tree_options.trex.plan import LegStructure
from tree_options.trex.supervised import (
    SupervisedIntent,
    SupervisedPaths,
    SupervisedRefused,
    grant_mandate,
    issue_permit,
    project_intent,
    record_intent,
    send,
)
from tree_options.trex.supervised_canary import (
    OperatorCanaryInputs,
    assignment_exposure,
    collect_canary_screening,
    modeled_margin,
)
from tree_options.trex.supervised_ibkr import (
    SUPERVISED_CLIENT_ID,
    IbkrSupervisedBroker,
    SupervisedEffect,
    effect_bytes,
    supervised_order_ref,
)

pytest.importorskip("ib_async")

ACCOUNT = "DU1234567"
STRATEGY = "operational-canary/1"
F = "20261016"
CON = {
    ("OPT", "SPY", F, 100.0, "P"): 100,
    ("OPT", "SPY", F, 95.0, "P"): 95,
    ("STK", "SPY", "", 0.0, ""): 1,
}
PROFILE = CapitalProfile(
    profile_id="canary-5k", revision=1, intended_capital=Decimal("5000"),
    risk_style="defined-risk", goals=("operational-canary",),
    allowed_strategy_versions=(STRATEGY,), max_loss_per_trade=Decimal("300"),
    max_open_loss=Decimal("1500"), max_daily_loss=Decimal("600"), horizon_days=30)


def _vertical(sid: str = "dv1", quantity: int = 1, kind: str = "debit_vertical",
              limit: str = "1.00") -> LegStructure:
    first, second = ("BUY", "SELL") if kind == "debit_vertical" else ("SELL", "BUY")
    return LegStructure(
        id=sid, underlying="SPY", kind=kind,
        legs=[{"right": "P", "action": first, "strike": "100", "expiry": date(2026, 10, 16)},
              {"right": "P", "action": second, "strike": "95", "expiry": date(2026, 10, 16)}],
        quantity=quantity, entry_date=date(2026, 9, 28), exit_deadline=date(2026, 10, 9),
        limit=limit, exits={"touch": False, "breach": False})


def _effect(structure: LegStructure | None = None, **overrides: Any) -> SupervisedEffect:
    structure = structure or _vertical()
    fields: dict[str, Any] = {
        "intent_id": "sup-001", "account_id": ACCOUNT, "structure": structure,
        "side": structure.open_side, "quantity": 1, "limit": Decimal("0.90"),
        "order_ref": supervised_order_ref("sup-001")}
    fields.update(overrides)
    return SupervisedEffect(**fields)


INPUTS = OperatorCanaryInputs(
    owner_epoch="sup-83-epoch-1", owner_healthy=True, assignment_plan_verified=True,
    protective_exit_ready=True, current_open_loss=Decimal("0"),
    realized_daily_loss=Decimal("0"))


def _live(tick_age_s: int | None = 5, client_id: int = SUPERVISED_CLIENT_ID
          ) -> tuple[IbkrSupervisedBroker, SupervisedGateway]:
    """A connected paper session with a two-sided quote ticked ``tick_age_s`` ago."""
    gw = SupervisedGateway(CON, ACCOUNT)
    ib = IbkrTrex(client_id=client_id)
    ib._ib = gw
    gw.quote(100, 2.00, 2.20)
    gw.quote(95, 1.10, 1.25)
    if tick_age_s is not None:
        stamp = shift_instant(datetime.now(UTC), -tick_age_s)
        for con in (100, 95):
            gw.tickers[con].time = stamp  # type: ignore[attr-defined]
    return IbkrSupervisedBroker(ib), gw


def _screen(broker: IbkrSupervisedBroker, effect: SupervisedEffect | None = None,
            inputs: OperatorCanaryInputs = INPUTS, mandate_account: str = ACCOUNT):
    return collect_canary_screening(broker, effect or _effect(), profile=PROFILE,
                                    mandate_account_id=mandate_account, inputs=inputs)


# ---------------------------------------------------------------- clear


def test_clean_paper_session_screens_clear():
    broker, gw = _live()
    screening = _screen(broker)
    assert screening.blockers == ()
    assert screening.clear
    assert screening.facts is not None
    assert screening.facts.worst_case_loss == Decimal("100.00")
    assert len(screening.screening_sha256) == 64
    assert gw.trades == [], "screening never places"


def test_screening_digest_moves_with_the_evidence():
    broker, gw = _live()
    first = _screen(broker)
    gw.quote(100, 2.05, 2.25)
    assert _screen(broker).screening_sha256 != first.screening_sha256


# ------------------------------------------------- observations -> blockers


def _bag(symbol: str, order_ref: str, order_id: int) -> SimpleNamespace:
    return SimpleNamespace(contract=SimpleNamespace(secType="BAG", symbol=symbol),
                           order=SimpleNamespace(orderRef=order_ref, orderId=order_id))


def test_same_underlying_positions_and_working_orders_block():
    broker, gw = _live()
    gw.position_rows.append(position_row(12, -2, account=ACCOUNT, symbol="SPY"))
    gw.foreign_open.append(_bag("SPY", "", 41))
    gw.foreign_open.append(_bag("SPY", supervised_order_ref("other"), 42))
    screening = _screen(broker)
    assert "legacy_book_not_flat" in screening.blockers
    assert screening.facts is not None
    assert (screening.facts.legacy_positions, screening.facts.legacy_working_orders) == (1, 1)
    assert screening.evidence["working_orders"] == ["41"], "supervised orders are not legacy"


def test_other_underlyings_do_not_block_but_stay_in_evidence():
    """Ruling 3b: the NVDA book does not block a SPY canary; it is still recorded."""
    broker, gw = _live()
    gw.position_rows.append(position_row(11, -3, account=ACCOUNT, symbol="NVDA"))
    gw.foreign_open.append(_bag("NVDA", "", 43))
    screening = _screen(broker)
    assert screening.clear
    assert screening.facts is not None
    assert (screening.facts.legacy_positions, screening.facts.legacy_working_orders) == (0, 0)
    assert screening.evidence["account_positions"] == ["NVDA:OPT:11:-3"]
    assert screening.evidence["account_working_orders"] == ["43"]
    assert screening.evidence["rulings"]["flat_book_scope"].startswith("2026-09-28 3b")


def test_other_accounts_positions_do_not_count():
    broker, gw = _live()
    gw.position_rows.append(position_row(11, -3, account="DU7777777", symbol="SPY"))
    assert _screen(broker).clear


@pytest.mark.parametrize("tick_age_s", [60, None])
def test_stale_or_untimed_quote_blocks(tick_age_s):
    broker, _ = _live(tick_age_s=tick_age_s)
    assert "quote_stale_or_future" in _screen(broker).blockers


def test_one_untimed_leg_makes_the_quote_stale():
    """A fresh leg cannot vouch for a leg that never ticked."""
    broker, gw = _live(tick_age_s=5)
    del gw.tickers[95].time  # type: ignore[attr-defined]
    assert "quote_stale_or_future" in _screen(broker).blockers


def test_quote_age_is_the_oldest_leg_tick():
    broker, gw = _live(tick_age_s=5)
    gw.tickers[95].time = shift_instant(datetime.now(UTC), -45)  # type: ignore[attr-defined]
    assert "quote_stale_or_future" in _screen(broker).blockers


def test_wide_vertical_margin_and_assignment_exposure_block():
    """Paper what-if margins are vacuous (all zero), so OUR model must bind;
    ruling 1a prices early assignment at the 60-wide gap (6000 > 5000)."""
    broker, gw = _live()
    gw.con_ids[("OPT", "SPY", F, 40.0, "P")] = 40
    gw.quote(40, 0.05, 0.10)
    gw.tickers[40].time = shift_instant(datetime.now(UTC), -5)  # type: ignore[attr-defined]
    wide = LegStructure(
        id="cv-wide", underlying="SPY", kind="credit_vertical",
        legs=[{"right": "P", "action": "SELL", "strike": "100", "expiry": date(2026, 10, 16)},
              {"right": "P", "action": "BUY", "strike": "40", "expiry": date(2026, 10, 16)}],
        quantity=1, entry_date=date(2026, 9, 28), exit_deadline=date(2026, 10, 9),
        limit="1.00", exits={"touch": False, "breach": False})
    screening = _screen(broker, _effect(wide, limit=Decimal("1.10")))
    assert screening.evidence["modeled_margin"] == "6000"
    assert screening.evidence["assignment_exposure"] == "6000"
    assert "broker_margin_exceeds_budget" in screening.blockers
    assert "assignment_exposure_exceeds_budget" in screening.blockers


def test_one_sided_quote_blocks():
    broker, gw = _live()
    gw.quote(95, None, None)
    blockers = _screen(broker).blockers
    assert "contract_or_quote_unverified" in blockers


def test_unreadable_broker_view_yields_no_facts():
    broker, gw = _live()
    gw.views_error = ConnectionError("gateway reset")
    screening = _screen(broker)
    assert screening.facts is None
    assert "broker_view_unreadable:all_client_open_orders" in screening.blockers


def test_session_guard_failures_block():
    broker, _ = _live(client_id=77)
    blockers = _screen(broker).blockers
    assert "wrong_client_id" in blockers
    assert "paper_account_mismatch_or_unverified" in blockers


def test_mandate_account_mismatch_blocks():
    broker, _ = _live()
    blockers = _screen(broker, mandate_account="DU0000001").blockers
    assert "paper_account_mismatch_or_unverified" in blockers
    assert "effect_account_not_mandate_account" in blockers


def test_structure_quantity_must_be_one():
    broker, _ = _live()
    assert "package_quantity_mismatch" in _screen(broker, _effect(_vertical(quantity=2))).blockers


def test_limit_above_the_cap_is_an_unverified_contract():
    broker, _ = _live()
    blockers = _screen(broker, _effect(limit=Decimal("1.50"))).blockers
    assert "contract_or_quote_unverified" in blockers


@pytest.mark.parametrize("change, blocker", [
    ({"owner_healthy": False}, "broker_owner_unhealthy"),
    ({"protective_exit_ready": False}, "protective_exit_unavailable"),
    ({"assignment_plan_verified": False}, "package_or_assignment_risk_unverified"),
    ({"current_open_loss": None}, "open_exposure_unknown"),
    ({"realized_daily_loss": Decimal("550")}, "daily_loss_cap_exceeded"),
])
def test_operator_inputs_are_never_inferred(change, blocker):
    broker, _ = _live()
    inputs = OperatorCanaryInputs(**{**INPUTS.__dict__, **change})
    assert blocker in _screen(broker, inputs=inputs).blockers


def test_margin_and_assignment_exposure_models():
    assert modeled_margin(_effect()) == Decimal("100.00")
    credit = _vertical(kind="credit_vertical", limit="1.00")
    assert modeled_margin(_effect(credit, limit=Decimal("1.10"))) == Decimal("500")
    # Ruling 1a: the 5-wide gap, not the ~$10k short-put notional at the 100 strike.
    assert assignment_exposure(_effect()) == Decimal("500")
    assert assignment_exposure(_effect(credit, limit=Decimal("1.10"))) == Decimal("500")


def test_assignment_exposure_is_computed_not_supplied():
    assert "temporary_assignment_exposure" not in OperatorCanaryInputs.__dataclass_fields__
    broker, _ = _live()
    screening = _screen(broker)
    assert screening.facts is not None
    assert screening.facts.temporary_assignment_exposure == Decimal("500")


# ------------------------------------------------------- the whole chain


def test_supervised_chain_rehearsal_on_fakes(tmp_path):
    """Mandate -> intent -> live screening -> permit bound to the screening
    digest -> send through the real IbkrTrex adapter -> acknowledged."""
    broker, gw = _live()
    paths = SupervisedPaths(tmp_path / "supervised")
    now = datetime.now(UTC)
    grant_mandate(paths, now=now, account_id=ACCOUNT, owner_epoch=INPUTS.owner_epoch,
                  strategy_version=STRATEGY, profile_digest="c" * 64, max_orders=1,
                  ttl_seconds=900, granted_by="operator-terminal")
    effect = _effect()
    payload = effect_bytes(effect)
    intent = SupervisedIntent(
        intent=OrderIntent(intent_id="sup-001", contract_id="BAG:dv1", side="BUY",
                           position_effect="OPEN_LONG", quantity=1, order_type="LIMIT",
                           limit_price=Decimal("0.90"), execution_style="package",
                           package_id="dv1", intent_created_at=now, source=STRATEGY,
                           source_sequence_id="seq-sup-001"),
        package_intent_sha256=_screen(broker).facts.intent_sha256,  # type: ignore[union-attr]
        created_at=now, send_deadline=shift_instant(now, 120))
    record_intent(paths, intent)

    # A blocked screening (a SPY position: same underlying) cannot mint a permit.
    gw.position_rows.append(position_row(12, -2, account=ACCOUNT, symbol="SPY"))
    blocked = _screen(broker)
    with pytest.raises(SupervisedRefused) as caught:
        issue_permit(paths, now=now, account_id=ACCOUNT, owner_epoch=INPUTS.owner_epoch,
                     intent=intent, canary_blockers=blocked.blockers,
                     effect_payload=payload, screening_sha256=blocked.screening_sha256)
    assert caught.value.reason == "canary_blockers"

    # Flat book: the screening clears and its digest is bound into the permit.
    gw.position_rows.clear()
    screening = _screen(broker)
    assert screening.clear
    permit = issue_permit(paths, now=now, account_id=ACCOUNT, owner_epoch=INPUTS.owner_epoch,
                          intent=intent, canary_blockers=screening.blockers,
                          effect_payload=payload, screening_sha256=screening.screening_sha256)
    assert permit.screening_sha256 == screening.screening_sha256

    receipt = send(paths, now=shift_instant(now, 1), permit_id=permit.permit_id,
                   effect_payload=payload, broker=broker)
    assert receipt["outcome"] == "acknowledged"
    (trade,) = gw.trades
    assert (trade.order.orderRef, trade.order.account) == ("trex:sup:sup-001", ACCOUNT)
    assert receipt["broker_order_id"] == str(trade.order.orderId)
    assert project_intent(paths, "sup-001").state == ExecutionState.ACKNOWLEDGED
