"""Schema unit tests: fail-closed validators across all record types."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from decimal import Decimal

import pydantic
import pytest

from tree_options.schemas import (
    CrossedQuoteError,
    DelistingRecord,
    DeliverableSpec,
    FeatureEvent,
    Fill,
    LedgerEntry,
    NakedShortProhibitedError,
    NonTradableConditionError,
    OptionContract,
    Order,
    PanelRow,
    QuoteEvent,
    SecurityMasterRecord,
    StaleQuoteError,
    TickerMappingRecord,
    ZeroSizeQuoteError,
    as_tradable,
)
from tree_options.schemas.trial import TrialRecord

T0 = datetime(2024, 3, 4, 21, 0, tzinfo=UTC)  # 16:00 ET close of 2024-03-04
T1 = datetime(2024, 3, 5, 14, 30, tzinfo=UTC)


def _feature(**over):
    kw = dict(
        security_id="SEC1",
        feature_name="momentum_20",
        value=0.5,
        observed_at=T0,
        available_at=T0,
        decision_at=T0,
        source="vendor_prices",
        source_record_id="rec-1",
        revision_id="r1",
    )
    kw.update(over)
    return FeatureEvent(**kw)


class TestFeatureEvent:
    def test_provenance_required(self):
        with pytest.raises(pydantic.ValidationError):
            _feature(source="")  # INV-01: provenance is mandatory
        for field in ("source", "source_record_id", "revision_id"):
            with pytest.raises(pydantic.ValidationError):
                _feature(**{field: ""})

    def test_naive_timestamp_rejected(self):
        from tree_options.schemas import NaiveTimestampError

        with pytest.raises((pydantic.ValidationError, NaiveTimestampError)):
            _feature(available_at=datetime(2024, 3, 4, 21, 0))

    def test_observed_after_available_rejected(self):
        with pytest.raises(pydantic.ValidationError):
            _feature(observed_at=T1, available_at=T0)

    def test_nan_value_rejected(self):
        with pytest.raises(pydantic.ValidationError):
            _feature(value=float("nan"))
        with pytest.raises(pydantic.ValidationError):
            _feature(value=math.inf)


def _security(**over):
    base = dict(
        security_id="SEC1",
        exchange="NASDAQ",
        listing_start=date(2015, 1, 2),
        source="listing_vendor",
        available_at=datetime(2015, 1, 2, 22, 0, tzinfo=UTC),
        ticker_mappings=(
            TickerMappingRecord(
                available_at=datetime(2020, 7, 1, tzinfo=UTC),
                security_id="SEC1",
                ticker="OLDA",
                effective_from=date(2015, 1, 2),
                effective_to=date(2020, 6, 30),
            ),
        ),
    )
    base.update(over)
    return SecurityMasterRecord(**base)


class TestSecurityMaster:
    def test_rename_keeps_identity_and_dated_mappings(self):
        sec = _security(
            ticker_mappings=(
                TickerMappingRecord(
                    available_at=datetime(2020, 7, 1, tzinfo=UTC),
                    security_id="SEC1",
                    ticker="OLDA",
                    effective_from=date(2015, 1, 2),
                    effective_to=date(2020, 6, 30),
                ),
                TickerMappingRecord(
                    available_at=datetime(2020, 7, 1, tzinfo=UTC),
                    security_id="SEC1",
                    ticker="NEWA",
                    effective_from=date(2020, 7, 1),
                ),
            )
        )
        assert sec.ticker_on(date(2019, 1, 2)) == "OLDA"
        assert sec.ticker_on(date(2021, 1, 4)) == "NEWA"
        with pytest.raises(KeyError):
            sec.ticker_on(date(2014, 12, 31))

    def test_overlapping_mappings_rejected(self):
        with pytest.raises(pydantic.ValidationError):
            _security(
                ticker_mappings=(
                    TickerMappingRecord(
                        available_at=datetime(2020, 7, 1, tzinfo=UTC),
                        security_id="SEC1",
                        ticker="A",
                        effective_from=date(2015, 1, 2),
                        effective_to=date(2020, 7, 15),
                    ),
                    TickerMappingRecord(
                        available_at=datetime(2020, 7, 1, tzinfo=UTC),
                        security_id="SEC1",
                        ticker="B",
                        effective_from=date(2020, 7, 1),
                    ),
                )
            )

    def test_delisting_requires_matching_listing_end(self):
        with pytest.raises(pydantic.ValidationError):
            _security(
                listing_end=date(2022, 6, 2),
                delisting=DelistingRecord(
                    delisting_session=date(2022, 6, 3),
                    reason="delisted",
                    final_price_available=True,
                ),
            )

    def test_delisted_name_in_universe_until_end(self):
        sec = _security(
            listing_end=date(2022, 6, 2),
            delisting=DelistingRecord(
                delisting_session=date(2022, 6, 2),
                reason="delisted",
                final_price_available=True,
                available_at=datetime(2022, 6, 2, 20, 0, tzinfo=UTC),
            ),
        )
        assert sec.listed_on(date(2022, 6, 1))
        assert not sec.listed_on(date(2022, 6, 3))


def _contract(**over):
    base = dict(
        contract_id="OPT1",
        option_root="NEWA",
        underlying_security_id="SEC1",
        expiration=date(2024, 5, 17),
        strike="50.00",
        call_put="C",
        listing_start=date(2024, 1, 2),
        listing_end=date(2024, 5, 17),
        deliverable=DeliverableSpec(shares_per_contract="100"),
        standard_contract_flag=True,
    )
    base.update(over)
    return OptionContract(**base)


class TestOptionContract:
    def test_standard_contract_ok(self):
        c = _contract()
        assert c.exists_on(date(2024, 3, 4))
        assert not c.exists_on(date(2023, 12, 29))
        assert not c.expired_on(date(2024, 5, 17))
        assert c.expired_on(date(2024, 5, 20))

    def test_nonstandard_requires_corporate_action(self):
        with pytest.raises(pydantic.ValidationError):
            _contract(
                standard_contract_flag=False,
                deliverable=DeliverableSpec(shares_per_contract="50"),
            )  # no corporate_action_id
        with pytest.raises(pydantic.ValidationError):
            _contract(
                standard_contract_flag=False,
                corporate_action_id="CA1",
                deliverable=DeliverableSpec(shares_per_contract="100"),
            )  # 100 shares is not an adjustment

    def test_adjusted_contract_traceable(self):
        c = _contract(
            standard_contract_flag=False,
            corporate_action_id="CA1",
            deliverable=DeliverableSpec(shares_per_contract="50", corporate_action_id="CA1"),
        )
        assert c.deliverable.corporate_action_id == c.corporate_action_id

    def test_listing_window_required(self):
        with pytest.raises(pydantic.ValidationError):
            _contract(listing_end=None)


def _quote(**over):
    base = dict(
        contract_id="OPT1",
        exchange_timestamp=T1,
        received_timestamp=T1,
        bid="1.00",
        ask="1.10",
        bid_size=10,
        ask_size=10,
        quote_condition="regular",
        source="nbbo_vendor",
    )
    base.update(over)
    return QuoteEvent(**base)


class TestQuotes:
    def test_raw_quote_accepts_crossed(self):
        q = _quote(bid="1.20", ask="1.10")  # crossed reality is representable raw
        assert q.bid > q.ask

    def test_raw_quote_rejects_inversion_and_negatives(self):
        with pytest.raises(pydantic.ValidationError):
            _quote(exchange_timestamp=T1, received_timestamp=T0)
        with pytest.raises(pydantic.ValidationError):
            _quote(bid="-0.01")

    def test_as_tradable_rejects_crossed(self):
        with pytest.raises(CrossedQuoteError):
            as_tradable(_quote(bid="1.20", ask="1.10"), execution_at=T1)

    def test_as_tradable_rejects_zero_size(self):
        with pytest.raises(ZeroSizeQuoteError):
            as_tradable(_quote(bid_size=0), execution_at=T1)

    def test_as_tradable_rejects_future_quote(self):
        with pytest.raises(StaleQuoteError):
            as_tradable(_quote(received_timestamp=T1), execution_at=T0)

    def test_as_tradable_rejects_over_age(self):
        from datetime import timedelta

        stale_ts = T1 - timedelta(seconds=901)
        old = _quote(exchange_timestamp=stale_ts, received_timestamp=stale_ts)
        with pytest.raises(StaleQuoteError):
            as_tradable(old, execution_at=T1)

    def test_as_tradable_rejects_condition(self):
        with pytest.raises(NonTradableConditionError):
            as_tradable(_quote(quote_condition="halted"), execution_at=T1)

    def test_as_tradable_passes_fresh_two_sided(self):
        tq = as_tradable(_quote(), execution_at=T1)
        assert tq.bid == tq.quote.bid


def _order(**over):
    base = dict(
        order_id="O1",
        contract_id="OPT1",
        side="buy",
        intent="open_long",
        quantity=1,
        decision_at=T0,
        decision_session=date(2024, 3, 4),
    )
    base.update(over)
    return Order(**base)


class TestOrderFill:
    def test_sell_to_open_rejected(self):
        with pytest.raises(NakedShortProhibitedError):
            _order(side="sell", intent="sell_to_open")

    def test_side_intent_pairing_enforced(self):
        with pytest.raises(NakedShortProhibitedError):
            _order(side="sell", intent="open_long")
        with pytest.raises(NakedShortProhibitedError):
            _order(side="buy", intent="close_long")

    def test_limit_consistency(self):
        with pytest.raises(pydantic.ValidationError):
            _order(order_type="limit")
        with pytest.raises(pydantic.ValidationError):
            _order(order_type="market", limit_price="1.00")

    def test_fill_money_math(self):
        f = Fill(
            fill_id="F1",
            order_id="O1",
            contract_id="OPT1",
            side="buy",
            quantity=3,
            price="1.10",
            multiplier=100,
            deliverable_shares_per_contract="100",
            fees="1.95",
            execution_at=T1,
            execution_session=date(2024, 3, 5),
        )
        assert f.notional() == Decimal("330.00")  # 1.10 * 3 * 100
        assert f.signed_cash() == Decimal("-330.00")
        assert f.notional() == pydantic.TypeAdapter(type(f.notional())).validate_python("330.00")
        assert str(f.signed_cash()) == "-330.00"


class TestTrialRecord:
    def test_requires_hypothesis(self):
        with pytest.raises(pydantic.ValidationError):
            _trial(hypothesis="short")

    def test_non_registered_requires_metrics(self):
        with pytest.raises(pydantic.ValidationError):
            _trial(status="completed")


def _trial(**over):
    base = dict(
        trial_id="TR-1",
        created_at=T0,
        hypothesis="baseline sector momentum ranks cross-sectionally",
        git_sha="a" * 40,
        config_hash="b" * 64,
        dataset_manifest_hash="c" * 64,
        hyperparameters={"lr": 0.1},
        scope_key="0.1.0:fold-1",
    )
    base.update(over)
    return TrialRecord(**base)


class TestLedgerEntry:
    def test_entry_shape(self):
        e = LedgerEntry(
            entry_id="E1",
            ts=T1,
            session=date(2024, 3, 5),
            kind="fee",
            amount="-0.65",
            contract_id="OPT1",
            ref_id="F1",
        )
        assert str(e.amount) == "-0.65"


class TestPanelRow:
    def test_row_groups_features_and_label_by_session(self):
        row = PanelRow(
            security_id="SEC1", decision_session=date(2024, 3, 4), features=(_feature(),)
        )
        assert row.label is None
        with pytest.raises(pydantic.ValidationError):
            PanelRow(security_id="SEC2", decision_session=date(2024, 3, 4), features=(_feature(),))
