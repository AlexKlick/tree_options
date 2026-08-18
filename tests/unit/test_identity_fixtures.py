"""INV-08/09 fixtures: ticker rename + delisting (fixture 3) and the
split-adjusted option deliverable (fixture 4).

The rename fixture proves: one security_id across two tickers, membership
usable through the delisting session, and a naive ticker join provably
spawning a spurious entity (two security_ids share one ticker symbol at
different times — a ticker-keyed panel would merge or split them wrongly).
"""

from __future__ import annotations

from datetime import date

import pytest

from tests.fixtures.contracts import split_adjusted_contract, standard_call
from tests.fixtures.security import renamed_and_delisted_security, successor_security_on_old_ticker


class TestRenameDelistingFixture:
    def test_identity_stable_across_rename(self):
        sec = renamed_and_delisted_security()
        assert sec.ticker_on(date(2024, 2, 1)) == "NEWM"
        assert sec.ticker_on(date(2024, 3, 14)) == "NEWM"
        assert sec.ticker_on(date(2024, 3, 15)) == "OLDA"  # rename boundary
        assert sec.ticker_on(date(2024, 6, 3)) == "OLDA"
        # one entity, two tickers, one security_id
        assert {m.ticker for m in sec.ticker_mappings} == {"NEWM", "OLDA"}
        assert {m.security_id for m in sec.ticker_mappings} == {"SEC-001"}

    def test_membership_through_delisting(self):
        sec = renamed_and_delisted_security()
        assert sec.listed_on(date(2024, 8, 2))  # final session: still a member
        assert not sec.listed_on(date(2024, 8, 5))  # after delisting
        with pytest.raises(KeyError):
            sec.ticker_on(date(2024, 8, 5))  # fails closed, no silent default

    def test_before_listing_fails_closed(self):
        sec = renamed_and_delisted_security()
        assert not sec.listed_on(date(2023, 12, 29))
        with pytest.raises(KeyError):
            sec.ticker_on(date(2023, 12, 29))

    def test_naive_ticker_join_spawns_spurious_entity(self):
        """The negative control: SEC-002 recycles the NEWM ticker later. A
        join keyed on ticker would attribute SEC-002's rows to SEC-001 (or
        split SEC-001's history at the rename). Keying on security_id — the
        protocol's identity_keys — keeps both entities intact."""
        sec1 = renamed_and_delisted_security()
        sec2 = successor_security_on_old_ticker()

        ticker_join: dict[str, str] = {}  # the WRONG data structure
        for sec in (sec1, sec2):
            for m in sec.ticker_mappings:
                ticker_join.setdefault(m.ticker, sec.security_id)
        # The ticker join collapses two distinct issuers into one key.
        assert len(ticker_join) < sum(len(s.ticker_mappings) for s in (sec1, sec2))

        identity_join = {
            (sec.security_id, m.ticker) for sec in (sec1, sec2) for m in sec.ticker_mappings
        }
        assert ("SEC-001", "NEWM") in identity_join
        assert ("SEC-001", "OLDA") in identity_join
        assert ("SEC-002", "NEWM") in identity_join  # same symbol, different issuer
        assert len(identity_join) == 3  # no entity lost, none merged

    def test_universe_config_demands_identity_keys(self, protocol):
        assert protocol.universe.ticker_is_not_identity is True
        assert protocol.universe.include_delisted is True
        assert "security_id" in protocol.universe.identity_keys


class TestSplitAdjustedContractFixture:
    def test_adjusted_contract_traces_its_corporate_action(self):
        contract, action = split_adjusted_contract()
        assert not contract.standard_contract_flag
        assert contract.corporate_action_id == action.corporate_action_id
        assert contract.deliverable.shares_per_contract == 150
        assert contract.deliverable.corporate_action_id == action.corporate_action_id
        assert action.action_type == "split" and action.ratio == 1.5

    def test_adjusted_contract_lifecycle_continues_after_split(self):
        contract, action = split_adjusted_contract()
        before = action.effective_session
        assert contract.exists_on(before)
        assert contract.exists_on(action.effective_session)  # split day itself
        assert contract.exists_on(contract.expiration)

    def test_standard_contract_unaffected_by_split_fixture(self):
        """The un-adjusted chain (pre-split strike, 100 shares) is a separate
        contract; the fixture pair models both sides of the corporate action."""
        plain = standard_call()
        adjusted, _ = split_adjusted_contract()
        assert plain.contract_id != adjusted.contract_id
        assert plain.standard_contract_flag
        assert plain.deliverable.shares_per_contract == 100

    def test_nonstandard_without_provenance_is_unrepresentable(self):
        """Structural backstop for INV-08: a 150-share deliverable with no
        corporate action is not a valid contract — silent corruption is not
        representable in the schema."""
        import re
        from decimal import Decimal

        from pydantic import ValidationError

        from tree_options.schemas.options import DeliverableSpec, OptionContract

        with pytest.raises(
            ValidationError, match=re.escape("nonstandard deliverable requires corporate_action_id")
        ):
            OptionContract(
                contract_id="OPT-C-2024-06-21-50-BAD",
                option_root="OPT",
                underlying_security_id="SEC-001",
                expiration=date(2024, 6, 21),
                strike=Decimal("33.33"),
                call_put="C",
                listing_start=date(2024, 1, 2),
                listing_end=date(2024, 6, 21),
                deliverable=DeliverableSpec(shares_per_contract=Decimal("150")),
                standard_contract_flag=False,
            )
