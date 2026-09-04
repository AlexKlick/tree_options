#!/usr/bin/env python
"""Deterministic mutation harness for the M0 gate (audit §6 contract).

Taxonomy (gate requires zero of everything except KILLED):
  KILLED           owning selectors FAILED behaviorally (pytest "FAILED" lines)
  SURVIVED         owning selectors passed under the mutant
  INVALID_MUTANT   mutant broke compilation, or pytest could not even collect
  TIMEOUT          owning selectors exceeded the per-mutant budget
  MUTATION_DRIFT   anchor not present exactly once (re-pin, never skip)
  HARNESS_ERROR    baseline failure, restore failure, unexpected crash

Runs in DISPOSABLE WORKTREE copies of the repo — the authoring tree is never
mutated. Baseline selectors must pass before each mutant (cached per
selector set within a shard; restore is byte-verified per mutant, and any
HARNESS_ERROR verdict clears the cache). The mutated file's pre-hash is
recorded; restoration is byte-verified. With --jobs N the registry is split
into N contiguous shards, each in its own disposable copy (threads; the work
is per-mutant subprocesses), and after all shards the full suite runs in
one shard's restored worktree to prove restoration. Outputs JSON and
Markdown tables; the report carries jobs + a selection block, and criterion
6 accepts only selection.mode == "full".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
G = "tests/guards"
U = "tests/unit"
P = "tests/properties"

MUTANTS = [
    dict(
        id="M01-availability-boundary",
        owner="test_inclusive_boundary_at_close",
        file="src/tree_options/guards/availability.py",
        anchor="if ev.available_at <= decision_at:",
        replacement="if ev.available_at < decision_at:",
        selectors=[f"{G}/test_fixture_filings.py"],
        invariant="INV-03/04 inclusive at-close availability",
    ),
    dict(
        id="M02-availability-gutted",
        owner="test_future_return_feature_rejected",
        file="src/tree_options/guards/availability.py",
        anchor="if ev.available_at <= decision_at:",
        replacement="if True:",
        selectors=[f"{G}/test_fixture_filings.py"],
        invariant="INV-03/04 future data rejected",
    ),
    dict(
        id="M03-same-close-instant",
        owner="test_next_session_close_instant_rejected",
        file="src/tree_options/guards/fills.py",
        anchor="effective_at > order.decision_at and exec_ord > decision_ord",
        replacement="True and exec_ord > decision_ord",
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="INV-10 instant-level same-close",
    ),
    dict(
        id="M04-same-close-ordinal",
        owner="test_same_session_160001_rejected",
        file="src/tree_options/guards/fills.py",
        anchor="effective_at > order.decision_at and exec_ord > decision_ord",
        replacement="effective_at > order.decision_at and True",
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="INV-10 ordinal-level same-close",
    ),
    dict(
        id="M05-decision-instant-not-close",
        owner="test_decision_at_must_be_session_close",
        file="src/tree_options/guards/fills.py",
        anchor="if order.decision_at != calendar_decision_close:",
        replacement="if False:",
        selectors=[f"{G}/test_fill_semantics_v2.py"],
        invariant="decision_at pinned to session close",
    ),
    dict(
        id="M06-contract-unknown-at-decision",
        owner="test_contract_must_exist_at_decision_time",
        file="src/tree_options/guards/fills.py",
        anchor="if not contract.exists_on(order.decision_session):",
        replacement="if False:",
        selectors=[f"{G}/test_fill_semantics_v2.py"],
        invariant="contract known at decision time",
    ),
    dict(
        id="M07-execution-not-session",
        owner="test_fill_on_non_session_rejected",
        file="src/tree_options/guards/fills.py",
        anchor="if not self.calendar.is_session(execution_session):",
        replacement="if False:",
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="fills only on real sessions",
    ),
    dict(
        id="M08-execution-instant-mismatch",
        owner="test_execution_instant_outside_session_rejected",
        file="src/tree_options/guards/fills.py",
        anchor="if not self.calendar.contains_instant(execution_session, effective_at):",
        replacement="if False:",
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="execution instant inside labeled session",
    ),
    dict(
        id="M09-nonstandard-deliverable-accepted",
        owner="test_nonstandard_deliverable_rejected_not_silent_100",
        file="src/tree_options/guards/fills.py",
        anchor="if not contract.standard_contract_flag:",
        replacement="if False:",
        selectors=[f"{G}/test_fill_semantics_v2.py"],
        invariant="multiplier never silently 100",
    ),
    dict(
        id="M10-side-size-inverted",
        owner="test_partial_fill_capped_at_quote_size",
        file="src/tree_options/guards/fills.py",
        anchor='displayed = tq.ask_size if order.side == "buy" else tq.bid_size',
        replacement='displayed = tq.bid_size if order.side == "buy" else tq.ask_size',
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="buy uses ask size, sell bid size",
    ),
    dict(
        id="M11-size-fraction-gutted",
        owner="test_fill_capped_at_fraction_of_displayed",
        file="src/tree_options/guards/fills.py",
        anchor="capacity = math.floor(self.fill_size_fraction * displayed)",
        replacement="capacity = displayed",
        selectors=[f"{G}/test_fill_semantics_v2.py"],
        invariant="displayed-size fraction enforced",
    ),
    dict(
        id="M12-unmarketable-limit-gutted",
        owner="test_unmarketable_buy_limit_rejected",
        file="src/tree_options/guards/fills.py",
        anchor='if order.side == "buy" and order.limit_price < price:',
        replacement="if False:",
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="unmarketable limit rejected",
    ),
    dict(
        id="M13-price-rounding-flipped",
        owner="test_exact_prices",
        file="src/tree_options/guards/fills.py",
        anchor="ticks = math.ceil(exact / 2)  # conservative: round the BUY price UP",
        replacement="ticks = math.floor(exact / 2)  # MUTATED",
        selectors=[f"{G}/test_fill_semantics_v2.py"],
        invariant="conservative tick rounding",
    ),
    dict(
        id="M14-quote-age-gutted",
        owner="test_over_age_quote_rejected",
        file="src/tree_options/schemas/market.py",
        anchor="if age > max_quote_age_seconds:",
        replacement="if False:",
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="stale quote rejected",
    ),
    dict(
        id="M15-future-quote-gutted",
        owner="test_future_quote_rejected_direct",
        file="src/tree_options/schemas/market.py",
        # Multi-line since G3: as_tradable_vwap carries the same single-line
        # publication check for bars, so the bare line matches twice. The
        # two-sided variant is pinned by its own message text.
        anchor=(
            "if q.received_timestamp > execution_at:\n"
            "        raise StaleQuoteError(\n"
            '            f"quote received {q.received_timestamp} after execution {execution_at}"\n'
            "        )"
        ),
        replacement=(
            "if False:\n"
            "        raise StaleQuoteError(\n"
            '            f"quote received {q.received_timestamp} after execution {execution_at}"\n'
            "        )"
        ),
        selectors=[f"{G}/test_fill_integrity_v2.py"],
        invariant="quote from the future rejected",
    ),
    dict(
        id="M16-crossed-gutted",
        owner="test_crossed_quote_rejected",
        file="src/tree_options/schemas/market.py",
        anchor="if q.bid > q.ask:",
        replacement="if False:",
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="crossed quote rejected",
    ),
    dict(
        id="M17-locked-gutted",
        owner="test_locked_quote_rejected_distinct_code",
        file="src/tree_options/schemas/market.py",
        anchor="if reject_locked and q.bid == q.ask:",
        replacement="if False:",
        selectors=[f"{G}/test_fill_semantics_v2.py"],
        invariant="locked quote rejected",
    ),
    dict(
        id="M18-nonpositive-gutted",
        owner="test_nonpositive_quote_rejected_distinct_code",
        file="src/tree_options/schemas/market.py",
        anchor="if q.bid <= 0 or q.ask <= 0:",
        replacement="if False:",
        selectors=[f"{G}/test_fill_semantics_v2.py"],
        invariant="nonpositive side rejected",
    ),
    dict(
        id="M19-quote-selection-reaches-back",
        owner="test_quote_stream_selection_monotone_in_time",
        file="src/tree_options/schemas/market.py",
        anchor="eligible = [q for q in quotes if q.received_timestamp <= execution_at]",
        replacement="eligible = list(quotes)",
        selectors=[f"{G}/test_fill_semantics_v2.py"],
        invariant="quote selection monotone in time",
    ),
    dict(
        id="M20-naive-timestamp-accepted",
        owner="test_naive_timestamp_rejected",
        file="src/tree_options/schemas/common.py",
        anchor="if v.tzinfo is None:",
        replacement="if False:",
        selectors=[f"{U}/test_schemas.py"],
        invariant="naive datetimes rejected",
    ),
    dict(
        id="M21-signed-cash-flipped",
        owner="test_fill_money_math",
        file="src/tree_options/schemas/trading.py",
        anchor='sign = -1 if self.side == "buy" else 1',
        replacement='sign = 1 if self.side == "buy" else -1',
        selectors=[f"{U}/test_schemas.py"],
        invariant="signed cash direction",
    ),
    dict(
        id="M22-fees-zeroed",
        owner="test_fill_carries_fees",
        file="src/tree_options/ledger/fees.py",
        anchor="return max(raw, self.minimum_per_order).quantize(FEE_TICK)",
        replacement='return Decimal("0")',
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="fees charged",
    ),
    dict(
        id="M23-duplicate-fill-accepted",
        owner="test_duplicate_fill_id_fails_closed",
        file="src/tree_options/ledger/book.py",
        anchor="if fill.fill_id in self._applied_fill_ids:",
        replacement="if False:",
        selectors=[f"{P}/test_ledger_properties.py"],
        invariant="duplicate fill rejected",
    ),
    dict(
        id="M24-ledger-underflow-gutted",
        owner="test_sell_beyond_position_fails_closed",
        file="src/tree_options/ledger/book.py",
        anchor="if held < fill.quantity:",
        replacement="if False:",
        selectors=[f"{P}/test_ledger_properties.py"],
        invariant="ledger underflow rejected",
    ),
    dict(
        id="M25-independent-oracle-broken",
        owner="test_conservation_oracle_independent_of_fill_methods",
        file="src/tree_options/ledger/book.py",
        anchor="cash += _primitive_cash(fill) - fill.fees",
        replacement="cash += fill.signed_cash() - fill.fees",
        selectors=[f"{P}/test_ledger_properties.py"],
        invariant="independent replay oracle",
    ),
    dict(
        id="M26-embargo-checker-gutted",
        owner="test_embargo_only_violation_detected",
        file="src/tree_options/splitting/checks.py",
        anchor="if first_eval - last_train <= gap:",
        replacement="if False:",
        selectors=[f"{P}/test_split_properties.py"],
        invariant="INV-06 embargo checked",
    ),
    dict(
        id="M27-anchor-checker-gutted",
        owner="test_anchor_violation_detected",
        file="src/tree_options/splitting/checks.py",
        anchor="if fold.train_sessions and min(fold.train_sessions) != base:",
        replacement="if False:",
        selectors=[f"{P}/test_split_properties.py"],
        invariant="INV-05 anchored train",
    ),
    dict(
        id="M28-coverage-checker-gutted",
        owner="test_coverage_violation_detected",
        file="src/tree_options/splitting/checks.py",
        anchor="if t in seen:",
        replacement="if False:",
        selectors=[f"{P}/test_split_properties.py"],
        invariant="INV-05 test blocks disjoint",
    ),
    dict(
        id="M29-session-grouping-gutted",
        owner="test_session_grouping_violation_detected",
        file="src/tree_options/splitting/checks.py",
        anchor="if len(roles) > 1:",
        replacement="if False:",
        selectors=[f"{P}/test_split_properties.py"],
        invariant="INV-05 same-session grouping",
    ),
    dict(
        id="M30-budget-cap-off-by-one",
        owner="test_scope_cap_32_enforced",
        file="src/tree_options/registry/budget.py",
        anchor="if registry.count_scope(scope_key) >= stored:",
        replacement="if registry.count_scope(scope_key) > stored:",
        selectors=[f"{U}/test_registry.py"],
        invariant="INV-13 32-cap exact",
    ),
    dict(
        id="M31-duplicate-trial-accepted",
        owner="test_duplicate_trial_id_rejected",
        file="src/tree_options/registry/sqlite.py",
        anchor='"INSERT INTO trials (trial_id, scope_key, scope_json, hypothesis,"',
        replacement='"INSERT OR IGNORE INTO trials (trial_id, scope_key, scope_json, hypothesis,"',
        selectors=[f"{U}/test_registry.py"],
        invariant="INV-13 duplicate id rejected",
    ),
    dict(
        id="M32-outcome-cas-gutted",
        owner="test_outcome_requires_running",
        file="src/tree_options/registry/sqlite.py",
        anchor="if cursor.rowcount != 1:",
        replacement="if False:",
        selectors=[f"{U}/test_registry.py"],
        invariant="REGISTERED->RUNNING->outcome ordering",
    ),
    dict(
        id="M33-scope-json-blanked",
        owner="test_stored_scope_json_verifiable",
        file="src/tree_options/registry/sqlite.py",
        anchor="scope.canonical_json(),",
        replacement='"{}",',
        selectors=[f"{U}/test_registry.py"],
        invariant="stored scope_json round-trips to the presented scope",
    ),
    dict(
        id="M34-candidate-future-input-accepted",
        owner="test_future_available_delta_not_evaluable",
        file="src/tree_options/candidates/filters.py",
        anchor="elif not snap.abs_delta.available_by(snap.decision_at):",
        replacement="elif False:",
        selectors=[f"{U}/test_candidate_filters.py"],
        invariant="future-available inputs rejected",
    ),
    dict(
        id="M35-candidate-acceptance-gutted",
        owner="test_fail_blocks_missing_and_future_alike",
        file="src/tree_options/candidates/filters.py",
        anchor="accepted = not any(r.status in {FAIL, NOT_EVALUABLE} for r in results)",
        replacement="accepted = True",
        selectors=[f"{U}/test_candidate_filters.py"],
        invariant="FAIL/NOT_EVALUABLE block acceptance",
    ),
    dict(
        id="M36-dte-gutted",
        owner="test_dte_calendar_day_convention_boundaries",
        file="src/tree_options/candidates/filters.py",
        anchor="if self.dte_min <= dte <= self.dte_max:",
        replacement="if True:",
        selectors=[f"{U}/test_candidate_filters.py"],
        invariant="DTE band enforced",
    ),
    dict(
        id="M37-protocol-hash-constant",
        owner="test_hash_changes_on_semantic_edit",
        file="src/tree_options/protocol/loader.py",
        anchor='return hashlib.sha256(canonical_json(protocol).encode("utf-8")).hexdigest()',
        replacement='return "0" * 64',
        selectors=[f"{U}/test_protocol_loader.py"],
        invariant="INV-01 protocol forks on semantic edit",
    ),
    dict(
        id="M38-calendar-checksum-ignored",
        owner="test_checksum_tamper_fails_closed",
        file="src/tree_options/time/calendar.py",
        anchor="if verify_checksum:",
        replacement="if False:",
        selectors=[f"{U}/test_calendar.py"],
        invariant="calendar tamper fails closed",
    ),
    dict(
        id="M39-decision-close-trusts-caller",
        owner="test_regular_close_on_early_close_session_rejected",
        file="src/tree_options/guards/fills.py",
        anchor="calendar_decision_close = self.calendar.session_close(order.decision_session)",
        replacement="calendar_decision_close = order.decision_at",
        selectors=[f"{G}/test_fill_integrity_v2.py"],
        invariant="decision close is calendar-derived, not caller-stamped",
    ),
    dict(
        id="M40-duplicate-order-accepted",
        owner="test_same_order_cannot_mint_two_fills",
        file="src/tree_options/guards/fills.py",
        anchor="if order.order_id in self._executed_orders and not partial_sequence:",
        replacement="if False:",
        selectors=[f"{G}/test_fill_integrity_v2.py"],
        invariant="one order mints one fill unless explicit partial sequence",
    ),
    dict(
        id="M41-lot-basis-stale",
        owner="test_average_cost_exact_after_partial_close",
        file="src/tree_options/ledger/book.py",
        # re-pinned M3 WS-C: the settlement lot walk added a second
        # cost_basis-reduction site, so the anchor now spans the
        # fill-path-specific block (fill.contract_id) to stay unique
        anchor=(
            "                head = self._lots[fill.contract_id][0]\n"
            "                take = min(head.quantity, remaining)\n"
            "                removed = (head.unit_price * take * head.multiplier).quantize(FEE_TICK)\n"
            "                cost_removed += removed\n"
            "                head.quantity -= take\n"
            "                head.cost_basis = (head.cost_basis - removed).quantize(FEE_TICK)"
        ),
        replacement=(
            "                head = self._lots[fill.contract_id][0]\n"
            "                take = min(head.quantity, remaining)\n"
            "                removed = (head.unit_price * take * head.multiplier).quantize(FEE_TICK)\n"
            "                cost_removed += removed\n"
            "                head.quantity -= take\n"
            "                head.cost_basis = head.cost_basis"
        ),
        selectors=[f"{P}/test_ledger_properties.py"],
        invariant="partial closes reduce lot basis",
    ),
    dict(
        id="M42-scope-derivation-ignored",
        owner="test_scope_key_must_derive_from_presented_scope",
        file="src/tree_options/registry/sqlite.py",
        anchor="if scope.scope_key() != record.scope_key:",
        replacement="if False:",
        selectors=[f"{U}/test_registry.py"],
        invariant="scope hash derives from presented TrialScope",
    ),
    dict(
        id="M43-security-future-mapping-visible",
        owner="test_january_decision_cannot_see_march_rename",
        file="src/tree_options/schemas/security.py",
        anchor=(
            "        for m in self.ticker_mappings:\n"
            "            if as_of is not None and m.available_at > as_of:\n"
            "                continue"
        ),
        replacement=(
            "        for m in self.ticker_mappings:\n"
            "            if False:\n"
            "                continue"
        ),
        selectors=[f"{U}/test_leakage_v2.py"],
        invariant="point-in-time security master hides future mappings (anchor re-pinned M2: sector_on now carries the same single-line shape)",
    ),
    dict(
        id="M44-volume-bypass-restored",
        owner="test_future_volume_with_not_applicable_flag_still_rejected",
        file="src/tree_options/candidates/filters.py",
        anchor="if snap.same_day_volume is not None:",
        replacement="if False:",
        selectors=[f"{U}/test_leakage_v2.py"],
        invariant="supplied volume always evaluated",
    ),
    dict(
        id="M45-off-tick-ask-accepted",
        owner="test_off_tick_ask_rejected",
        file="src/tree_options/schemas/market.py",
        anchor='assert_on_tick(q.ask, "ask")',
        replacement="pass",
        selectors=[f"{G}/test_fill_integrity_v2.py"],
        invariant="off-tick ask rejected",
    ),
    dict(
        id="M47-off-tick-bid-accepted",
        owner="test_off_tick_bid_rejected",
        file="src/tree_options/schemas/market.py",
        anchor='assert_on_tick(q.bid, "bid")',
        replacement="pass",
        selectors=[f"{G}/test_fill_integrity_v2.py"],
        invariant="off-tick bid rejected",
    ),
    dict(
        id="M46-fit-on-eval-undetected",
        owner="test_fit_that_included_eval_is_detected",
        file="src/tree_options/guards/fitting.py",
        anchor="overlap = fit & forbidden",
        replacement="overlap = frozenset()",
        selectors=["tests/guards/test_fitting_guard.py"],
        invariant="INV-07 fit-on-train-only detected",
    ),
    dict(
        id="M48-partial-remaining-unbounded",
        owner="test_partial_sequence_cannot_exceed_order_quantity",
        file="src/tree_options/guards/fills.py",
        anchor="remaining = order.quantity - already_filled",
        replacement="remaining = order.quantity",
        selectors=[f"{G}/test_fill_integrity_v2.py"],
        invariant="partial chains bounded by REMAINING quantity (overfill occurs if unbounded)",
    ),
    dict(
        id="M49-security-record-future-visible",
        owner="test_january_cannot_see_record_that_arrived_in_march",
        file="src/tree_options/schemas/security.py",
        anchor="return as_of is None or self.available_at <= as_of",
        replacement="return True",
        selectors=[f"{U}/test_leakage_v2.py"],
        invariant="master record invisible before its own available_at",
    ),
    dict(
        id="M50-deliverable-action-id-ignored",
        owner="test_standard_contract_with_deliverable_action_id_rejected",
        file="src/tree_options/schemas/options.py",
        anchor="if self.deliverable.corporate_action_id is not None:",
        replacement="if False:",
        selectors=[f"{U}/test_leakage_v2.py"],
        invariant="standard deliverable carries no action provenance",
    ),
    dict(
        id="M51-snapshot-incoherence-accepted",
        owner="test_mismatched_expiration_not_evaluable",
        file="src/tree_options/candidates/filters.py",
        anchor="snap.expiration != snap.contract.expiration",
        replacement="False",
        selectors=[f"{U}/test_leakage_v2.py"],
        invariant="snapshot fields must agree with the contract object",
    ),
    dict(
        id="M52-finite-listing-end-never-honored",
        owner="test_listing_end_honored_once_passed",
        file="src/tree_options/schemas/security.py",
        anchor="self.listing_end is not None and as_of.date() > self.listing_end",
        replacement="False",
        selectors=[f"{U}/test_leakage_v2.py"],
        invariant="finite listing_end with no delisting is honored once passed",
    ),
    dict(
        id="M53-order-rebind-accepted",
        owner="test_rebound_order_id_rejected",
        file="src/tree_options/guards/fills.py",
        anchor="if bound is not None and bound != order:",
        replacement="if False:",
        selectors=[f"{G}/test_fill_integrity_v2.py"],
        invariant="an order_id is bound to the order that first minted a fill",
    ),
    dict(
        id="M54-cap-type-gutted",
        owner="test_budget_cap_must_be_an_integer",
        file="src/tree_options/registry/budget.py",
        anchor="if isinstance(cap, bool) or not isinstance(cap, int):",
        replacement="if False:",
        selectors=[f"{U}/test_registry.py"],
        invariant="scope cap is an integer commitment (NaN cap would disable the cap)",
    ),
    dict(
        id="M55-cap-storage-replaced",
        owner="test_budget_cap_cannot_exceed_protocol_maximum",
        file="src/tree_options/registry/budget.py",
        anchor="self._cap = cap",
        replacement="self._cap = MAX_SCOPE_CAP",
        selectors=[f"{U}/test_registry.py"],
        invariant="the constructed cap is the stored cap (read-only storage fidelity)",
    ),
    dict(
        id="M56-protocol-cap-lax",
        owner="test_max_registered_configs_is_strict",
        file="src/tree_options/protocol/schema.py",
        anchor="max_registered_configs: int = Field(gt=0, strict=True)",
        replacement="max_registered_configs: int = Field(gt=0)",
        selectors=[f"{U}/test_protocol_loader.py"],
        invariant="protocol cap field is strict (bool/str/float never coerce into the commitment)",
    ),
    dict(
        id="M57-cap-revalidation-gutted",
        owner="test_poisoned_backing_field_fails_closed",
        file="src/tree_options/registry/budget.py",
        anchor="if not valid:",
        replacement="if False:",
        selectors=[f"{U}/test_registry.py"],
        invariant="tampered cap fails closed at the enforcement point (registration refuses)",
    ),
    dict(
        id="M58-supplied-budget-dropped",
        owner="test_two_connections_cannot_exceed_cap",
        file="src/tree_options/registry/sqlite.py",
        anchor="self._budget = budget or _TB()",
        replacement="self._budget = _TB()",
        selectors=[f"{U}/test_registry.py"],
        invariant="the registry enforces the SUPPLIED budget (a tightened cap cannot be swapped for the default)",
    ),
    dict(
        id="M59-commitment-equality-gutted",
        owner="test_committed_cap_cannot_be_loosened_mid_scope",
        file="src/tree_options/registry/sqlite.py",
        anchor="if committed is not None and int(committed[0]) != cap:",
        replacement="if False:",
        selectors=[f"{U}/test_registry.py"],
        invariant="the live cap must equal the cap COMMITTED to storage (in-range loosening refuses)",
    ),
    dict(
        id="M60-commitment-read-misses",
        owner="test_swapped_budget_reference_refuses",
        file="src/tree_options/registry/sqlite.py",
        anchor='"SELECT cap FROM scope_commitments WHERE scope_key = ?",',
        replacement='"SELECT cap FROM scope_commitments WHERE scope_key = ? AND cap < 0",',
        selectors=[f"{U}/test_registry.py"],
        invariant=(
            "the recorded commitment is READ at every registration (a missed read re-opens"
            " loosening via a swapped budget; the mutant EXECUTES a valid query that"
            " matches no row — placeholder and binding counts unchanged — rather than"
            " crashing, so a kill is behavioral only)"
        ),
    ),
    dict(
        id="M61-migration-backfill-empty",
        owner="test_migrated_scope_is_committed_at_open",
        file="src/tree_options/registry/sqlite.py",
        anchor='" SELECT DISTINCT scope_key, ?, ? FROM trials",',
        replacement='" SELECT DISTINCT scope_key, ?, ? FROM trials WHERE 0",',
        selectors=[f"{U}/test_registry.py"],
        invariant="a scope populated before the commitment table is COMMITTED at open (no backfill re-opens its first post-upgrade registration)",
    ),
    dict(
        id="M62-duplicate-bar-accepted",
        owner="test_duplicate_bars_rejected",
        file="src/tree_options/data/quality.py",
        anchor="if key in seen:",
        replacement="if False:",
        selectors=[f"{U}/test_data_quality.py"],
        invariant="M1-E duplicate (security, session) bars are rejected (duplicates inflate panels)",
    ),
    dict(
        id="M63-split-discontinuity-gutted",
        owner="test_undeclared_price_discontinuity_rejected",
        file="src/tree_options/data/quality.py",
        anchor="if factor <= SPLIT_FACTOR_INVERSE or factor >= SPLIT_FACTOR_BOUND:",
        replacement="if False:",
        selectors=[f"{U}/test_data_quality.py"],
        invariant="M1-E an overnight factor at/beyond the split bounds requires a covering action (unrepresented splits corrupt labels)",
    ),
    dict(
        id="M64-manifest-content-gutted",
        owner="test_manifest_tampering_is_detected",
        file="src/tree_options/data/manifest.py",
        anchor="digest.update(canonical_bytes(bar))",
        replacement='digest.update(b"")',
        selectors=[f"{U}/test_data_quality.py"],
        invariant="M1-D the manifest content hash is bound to the bars (a post-ingest row swap must not survive verification)",
    ),
    dict(
        id="M65-current-ticker-join-accepted",
        owner="test_ticker_resolution_is_point_in_time",
        file="src/tree_options/data/resolve.py",
        anchor="if m.available_at > as_of:",
        replacement="if False:",
        selectors=[f"{U}/test_data_ingest.py"],
        invariant="M1-C ticker resolution is point-in-time (a mapping announced after as_of is invisible — the current-ticker join is refused)",
    ),
    dict(
        id="M66-future-bar-visible",
        owner="test_future_bar_is_invisible",
        file="src/tree_options/data/authority.py",
        anchor="return bars[: bisect_right(available, decision_at)]",
        replacement="return bars",
        selectors=[f"{U}/test_data_authority.py"],
        invariant=(
            "M1-C the authority never returns a bar published after decision_at "
            "(future data is invisible at the read gate); anchor re-pinned "
            "2026-08-19 — the M2-proper C lane replaced the linear filter with "
            "the monotone bisect fast path, same leak on the new shape"
        ),
    ),
    dict(
        id="M67-universe-survivorship-gutted",
        owner="test_universe_is_point_in_time_not_survivors",
        file="src/tree_options/data/authority.py",
        anchor="if record.listed_on(decision_at.date(), as_of=decision_at)",
        replacement="if True",
        selectors=[f"{U}/test_data_authority.py"],
        invariant="M1-C universe membership is point-in-time (delisted names leave, pre-IPO names never enter — no current-survivor filtering)",
    ),
    dict(
        id="M68-master-content-gutted",
        owner="test_master_tampering_is_detected",
        file="src/tree_options/data/manifest.py",
        anchor="digest.update(canonical_bytes(record))",
        replacement='digest.update(b"")',
        selectors=[f"{U}/test_data_quality.py"],
        invariant="M1-D the manifest content hash binds the MASTER records (a post-ingest listing swap must not survive verification)",
    ),
    dict(
        id="M69-resolver-record-visibility-gutted",
        owner="test_resolver_respects_master_record_availability",
        file="src/tree_options/data/resolve.py",
        anchor="if record_available > as_of:",
        replacement="if False:",
        selectors=[f"{U}/test_data_ingest.py"],
        invariant="M1-C a mapping inside a not-yet-knowable master record is invisible (record.available_at gates the whole record)",
    ),
    dict(
        id="M70-snapshot-rebind-accepted",
        owner="test_snapshot_identity_is_bound",
        file="src/tree_options/data/quality.py",
        anchor="identity_ok = snapshot.snapshot_id == m.snapshot_id and rows_identity",
        replacement="identity_ok = True and rows_identity",
        selectors=[f"{U}/test_data_quality.py"],
        invariant="M1-D the outer snapshot id cannot be rebound post-ingest (outer, manifest, and per-row ids must agree)",
    ),
    dict(
        id="M71-sector-leak-window-open",
        owner="test_leak_window_returns_prior_sector",
        file="src/tree_options/schemas/security.py",
        anchor=(
            "        best: SectorMappingRecord | None = None\n"
            "        for m in self.sector_mappings:\n"
            "            if as_of is not None and m.available_at > as_of:\n"
            "                continue"
        ),
        replacement=(
            "        best: SectorMappingRecord | None = None\n"
            "        for m in self.sector_mappings:\n"
            "            if False:\n"
            "                continue"
        ),
        selectors=[f"{U}/test_sector_pit.py"],
        invariant="M2-A sector classifications are availability-gated: a reclassification between effective_from and available_at must stay invisible",
    ),
    dict(
        id="M72-seed-stream-shifted",
        owner="test_small_dev_worlds_reproduce_byte_exact",
        file="src/tree_options/synth/generate.py",
        anchor='base = f"{spec.world_id}/{spec.seed}/seat/{self.security_id}"',
        replacement='base = f"{spec.world_id}/{spec.seed + 1}/seat/{self.security_id}"',
        selectors=[f"{U}/test_world_registry.py"],
        invariant="M2-B/C a world is pinned to its registered seed: byte-exact registry reproduction fails if stream seeding drifts",
    ),
    dict(
        id="M73-alpha-injection-gutted",
        owner="test_null_and_alpha_worlds_share_structure",
        file="src/tree_options/synth/generate.py",
        anchor="ret += spec.alpha.coefficient * (seat.prev_ret - cross_mean)",
        replacement="ret += 0.0",
        selectors=[f"{U}/test_synth_generate.py"],
        invariant="M2-B the planted effect actually moves closes: an alpha world must differ from its same-seed null world",
    ),
    dict(
        id="M74-publication-hour-shifted",
        owner="test_publication_instant_discipline",
        file="src/tree_options/synth/generate.py",
        anchor="datetime(session.year, session.month, session.day, hour, 0, tzinfo=UTC)",
        replacement="datetime(session.year, session.month, session.day, hour - 1, 0, tzinfo=UTC)",
        selectors=[f"{U}/test_synth_generate.py"],
        invariant="M2-B every row publishes at the spec's fixed 23:00 UTC instant (the availability gates key on it; round-1 P2-2 re-pinned from hour+1, which crashed construction instead of testing detection)",
    ),
    dict(
        id="M75-recycle-truth-gutted",
        owner="test_lifecycle_scenarios_present",
        file="src/tree_options/synth/generate.py",
        anchor="recycled.append(ticker)",
        replacement="recycled.clear()",
        selectors=[f"{U}/test_synth_generate.py"],
        invariant="M2-B the truth sidecar records ticker recycling (the fixture's INV-08 scenario)",
    ),
    dict(
        id="M76-initial-cohort-unlisted",
        owner="test_small_dev_worlds_reproduce_byte_exact",
        file="src/tree_options/synth/generate.py",
        anchor="for seat in seats[:n_initial]:",
        replacement="for seat in seats[:0]:",
        selectors=[f"{U}/test_world_registry.py"],
        invariant="M2-B the initial cohort lists on the first session: registry worlds reproduce only with the listed universe",
    ),
    dict(
        id="M77-bankruptcy-bound-over-gate",
        owner="test_generated_world_ingests_and_verifies",
        file="src/tree_options/synth/generate.py",
        anchor="BANKRUPTCY_LOSS = (0.40, 0.49)",
        replacement="BANKRUPTCY_LOSS = (0.80, 0.95)",
        selectors=[f"{U}/test_synth_generate.py"],
        invariant="M2-B/E terminal crash losses stay under the 2x undeclared-discontinuity quality gate",
    ),
    dict(
        id="M78-split-override-not-exact",
        owner="test_generated_world_ingests_and_verifies",
        file="src/tree_options/synth/generate.py",
        anchor="new_close = _cents(seat.close * override_today)",
        replacement="new_close = _cents(seat.close)",
        selectors=[f"{U}/test_synth_generate.py"],
        invariant="M2-B/E split sessions derive the close exactly from the declared ratio (ratio-match quality gate)",
    ),
    dict(
        id="M79-split-floor-suppression-gutted",
        owner="test_hostile_rate_spec_stays_gate_clean",
        file="src/tree_options/synth/generate.py",
        anchor='RATIO_FLOOR = Decimal("1.00")',
        replacement='RATIO_FLOOR = Decimal("0.00")',
        selectors=[f"{U}/test_synth_generate.py"],
        invariant="M2 round-1 P1-2: ratio events that would floor-clamp the close are suppressed (the floor exists), so any accepted spec generates a gate-clean world",
    ),
    dict(
        id="M80-session-return-clamp-removed",
        owner="test_session_returns_bounded_under_gate",
        file="src/tree_options/synth/generate.py",
        anchor="return max(-DAILY_RET_LIMIT, min(DAILY_RET_LIMIT, ret))",
        replacement="return ret",
        selectors=[f"{U}/test_synth_generate.py"],
        invariant="M2 round-1: undeclared overnight moves are clamped strictly inside the 2x discontinuity gate bound",
    ),
    dict(
        id="M81-min-close-floor-removed",
        owner="test_minimum_close_floor",
        file="src/tree_options/synth/generate.py",
        anchor="return max(x.quantize(CENT, rounding=ROUND_HALF_UP), MIN_CLOSE)",
        replacement="return x.quantize(CENT, rounding=ROUND_HALF_UP)",
        selectors=[f"{U}/test_synth_generate.py"],
        invariant="M2 round-2 P1-1: closes never quantize below $1.00, where cent rounding is too small to land on the 0.5x/2x gate bounds",
    ),
    dict(
        id="M83-application-guard-gutted",
        owner="test_hostile_specs_verify_across_seeds",
        file="src/tree_options/synth/generate.py",
        anchor="if seat.base_close * announced.factor < RATIO_FLOOR:",
        replacement="if False:",
        selectors=[f"{U}/test_synth_generate.py"],
        invariant="M2 round-3 P1-1: ratio events are decided at APPLICATION time against the actual session price — never canceled-blind at announcement",
    ),
    dict(
        id="M84-alpha-drift-wall-removed",
        owner="test_alpha_drift_wall_bounds_wobble",
        file="src/tree_options/synth/generate.py",
        anchor="                if new_close > drift_up:\n                    new_close = drift_up",
        replacement="                if False:\n                    new_close = drift_up",
        selectors=[f"{U}/test_synth_generate.py"],
        invariant="M2 round-4 P1-1: cumulative alpha drift is walled so the alpha-vs-base wobble and every combined session factor stay strictly inside the gate bounds and the ratio-match tolerance",
    ),
    # ---- M2-proper (owner-scoped, one invariant per new-code seam) --------
    dict(
        id="M85-label-window-extended",
        owner="test_h1_label_value_window_and_provenance",
        file="src/tree_options/labels/build.py",
        anchor="end_ordinal = base_ordinal + horizon_sessions",
        replacement="end_ordinal = base_ordinal + horizon_sessions + 1",
        selectors=[f"{U}/test_labels_build.py"],
        invariant="M2-B the label window is exactly H sessions after the base bar (b+1, b+H); extending it changes the label value",
    ),
    dict(
        id="M86-staleness-skip-gutted",
        owner="test_no_label_when_stale_history_straddles_decision",
        file="src/tree_options/labels/build.py",
        anchor="if base_ordinal + 1 != decision_ordinal:",
        replacement="if False:",
        selectors=[f"{U}/test_labels_build.py"],
        invariant="M2-B a stale base bar (last visible bar older than d-1) yields NO label, never a lookback-through-the-gap label",
    ),
    dict(
        id="M87-total-return-gutted",
        owner="test_h1_label_across_split_is_total_return",
        file="src/tree_options/labels/build.py",
        anchor=(
            "ratio_factor *= Decimal(act.ratio_numerator) / Decimal(\n"
            "                            act.ratio_denominator\n"
            "                        )"
        ),
        replacement="ratio_factor *= Decimal(1)",
        selectors=[f"{U}/test_labels_build.py"],
        invariant="M2-B split/reverse/stock-dividend labels are total-return adjusted (uniform n/d wealth multiplier); raw closes would corrupt every in-window action",
    ),
    dict(
        id="M88-cash-dividend-dropped",
        owner="test_cash_dividend_held_as_cash_in_value",
        file="src/tree_options/labels/build.py",
        anchor="wealth = (end.close * ratio_factor + cash_total) / base.close",
        replacement="wealth = (end.close * ratio_factor) / base.close",
        selectors=[f"{U}/test_labels_build.py"],
        invariant="M2-B cash dividends inside the window are held unreinvested and enter the label value",
    ),
    dict(
        id="M89-window-gap-gutted",
        owner="test_no_label_when_window_has_a_gap",
        file="src/tree_options/labels/build.py",
        anchor="if any(o not in bar_at_ordinal for o in range(base_ordinal + 1, end_ordinal + 1)):",
        replacement="if False:",
        selectors=[f"{U}/test_labels_build.py"],
        invariant="M2-B a gap inside the label window (lapse/delisting) yields NO label, never a span-the-gap label",
    ),
    dict(
        id="M90-non-monotone-publication-accepted",
        owner="test_non_monotone_publication_fails_closed",
        file="src/tree_options/labels/build.py",
        anchor="if later.available_at < earlier.available_at:",
        replacement="if False:",
        selectors=[f"{U}/test_labels_build.py"],
        invariant="M2-B the two-pointer visibility walk fails closed when publication order does not follow session order",
    ),
    dict(
        id="M91-contiguity-gutted",
        owner="test_lapse_inside_lookback_means_absent",
        file="src/tree_options/data/authority.py",
        anchor="if any(later - earlier != 1 for earlier, later in pairwise(positions)):",
        replacement="if False:",
        selectors=[f"{U}/test_features_v2.py"],
        invariant="M2-C calendar-contiguous lookbacks only: a lapse inside the window makes the feature absent, never imputed across the gap",
    ),
    dict(
        id="M92-momentum-horizon-shifted",
        owner="test_momentum_and_liquidity_values",
        file="src/tree_options/data/authority.py",
        anchor="tail = _contiguous_tail(bars, horizon + 1, ordinals)",
        replacement="tail = _contiguous_tail(bars, horizon, ordinals)",
        selectors=[f"{U}/test_features_v2.py"],
        invariant="M2-C mom_H is log(c_b / c_{b-H}) over exactly H+1 aligned bars; a shifted window changes every momentum value",
    ),
    dict(
        id="M93-dol-vol-mean-denominator-shifted",
        owner="test_momentum_and_liquidity_values",
        file="src/tree_options/data/authority.py",
        anchor="value=float(total / Decimal(_DOL_VOL_WINDOW)),",
        replacement="value=float(total / Decimal(_DOL_VOL_WINDOW - 1)),",
        selectors=[f"{U}/test_features_v2.py"],
        invariant="M2-C dol_vol_20 is the mean over exactly the 20 aligned bars",
    ),
    dict(
        id="M94-fit-guard-registration-gutted",
        owner="test_refit_is_refused",
        file="src/tree_options/models/pipeline.py",
        anchor="self.guard.fit_on(self.artifact, fit_sessions)",
        replacement="pass",
        selectors=[f"{U}/test_models_pipeline.py"],
        invariant="M2-D fit-once and fit-session registration are guard-enforced INSIDE the pipeline (INV-07 discharged)",
    ),
    dict(
        id="M95-fit-eval-disjointness-gutted",
        owner="test_fit_on_eval_sessions_is_detected_at_score",
        file="src/tree_options/models/pipeline.py",
        anchor="self.guard.assert_fit_excludes(self.artifact, target_sessions)",
        replacement="pass",
        selectors=[f"{U}/test_models_pipeline.py"],
        invariant="M2-D scoring a pipeline on sessions it was fitted on is detected and refused at score time",
    ),
    dict(
        id="M96-score-standardizer-leaks-eval-stats",
        owner="test_standardizer_uses_train_statistics_only",
        file="src/tree_options/models/pipeline.py",
        anchor="z = (x - self._mean) / self._scale",
        replacement=(
            "z = (x - x.mean(axis=0)) / np.where(x.std(axis=0) == 0.0, 1.0, x.std(axis=0))"
        ),
        selectors=[f"{U}/test_models_pipeline.py"],
        invariant="M2-D the standardizer carries TRAIN statistics only; recomputing them on eval rows is leakage",
    ),
    dict(
        id="M97-blas-pin-gutted",
        owner="test_package_import_pins_blas_before_numpy",
        file="src/tree_options/models/determinism.py",
        anchor='os.environ[var] = "1"',
        replacement='os.environ[var] = "4"',
        selectors=[f"{U}/test_models_pipeline.py"],
        invariant="M2-D single-threaded BLAS is forced for every knob; multi-thread reduction order breaks byte-identical determinism",
    ),
    dict(
        id="M98-average-rank-ties-gutted",
        owner="test_spearman_assigns_average_ranks_for_ties",
        file="src/tree_options/evaluation/stats.py",
        anchor="average = (start + 1 + stop) / 2.0",
        replacement="average = float(stop)",
        selectors=[f"{U}/test_evaluation_stats.py"],
        invariant="M2-E Spearman coefficients use deterministic average ranks for ties",
    ),
    dict(
        id="M99-unevaluable-fabricated-zero",
        owner="test_spearman_returns_none_for_unevaluable_cross_sections",
        file="src/tree_options/evaluation/stats.py",
        anchor="    if len(x) < 2:\n        return None",
        replacement="    if len(x) < 2:\n        return 0.0",
        selectors=[f"{U}/test_evaluation_stats.py"],
        invariant="M2-E unevaluable cross-sections are None, never a fabricated zero that would dilute pooled statistics",
    ),
    dict(
        id="M100-binomial-boundary-term-dropped",
        owner="test_exact_binomial_upper_tail_and_threshold",
        file="src/tree_options/evaluation/stats.py",
        anchor="for observed in range(successes, trials + 1)",
        replacement="for observed in range(successes + 1, trials + 1)",
        selectors=[f"{U}/test_evaluation_stats.py"],
        invariant="M2-E the exact binomial upper tail includes its boundary term P[X = successes]; the FP threshold is exact",
    ),
    dict(
        id="M101-t-statistic-population-variance",
        owner="test_one_sample_t_statistic_uses_sample_variance",
        file="src/tree_options/evaluation/stats.py",
        anchor="variance = math.fsum((value - mean) ** 2 for value in sample) / (len(sample) - 1)",
        replacement="variance = math.fsum((value - mean) ** 2 for value in sample) / len(sample)",
        selectors=[f"{U}/test_evaluation_stats.py"],
        invariant="M2-E the one-sample t uses sample variance (ddof=1)",
    ),
    dict(
        id="M102-fold-filter-gutted",
        owner="test_no_folds_fails_before_registration",
        file="src/tree_options/trials/run.py",
        anchor="folds = [fold for fold in folds if fold.test_sessions <= world_sessions]",
        replacement="folds = list(folds)",
        selectors=[f"{U}/test_trials_run.py"],
        invariant="M2-F folds whose test blocks run past the world's session range are dropped whole, never half-counted",
    ),
    dict(
        id="M103-fail-through-gutted",
        owner="test_artifact_write_failure_marks_trial_failed",
        file="src/tree_options/trials/run.py",
        anchor='registry.fail(trial_id, f"{type(exc).__name__}: {exc}", at=clock())',
        replacement="pass",
        selectors=[f"{U}/test_trials_run.py"],
        invariant="M2-F a trial never ends in limbo: any execution error marks the trial FAILED before re-raising",
    ),
    dict(
        id="M104-next-open-shifted-to-same-session",
        owner="test_top_quintile_executes_only_at_next_session_open",
        file="src/tree_options/backtest/equity.py",
        anchor="expected = self.calendar.nth_after(decision_session, 1)",
        replacement="expected = self.calendar.nth_after(decision_session, 0)",
        selectors=[f"{U}/test_equity_backtest.py"],
        invariant="M2-G decisions execute at the NEXT session's open, never the decision session's own open (look-ahead)",
    ),
    dict(
        id="M105-fee-zeroed",
        owner="test_fixed_five_basis_point_fee_is_applied_per_side",
        file="src/tree_options/backtest/equity.py",
        anchor="return (price * quantity * self.rate).quantize(FEE_TICK, rounding=ROUND_HALF_UP)",
        replacement="return Decimal(0)",
        selectors=[f"{U}/test_equity_backtest.py"],
        invariant="M2-G the campaign-fixed 5bp/side fee is charged on every ordinary trade",
    ),
    dict(
        id="M106-conversion-ratio-inverted",
        owner="test_split_conversion_preserves_value_and_fifo_conservation_to_penny",
        file="src/tree_options/backtest/equity.py",
        anchor="ratio = Decimal(ratio_numerator) / Decimal(ratio_denominator)",
        replacement="ratio = Decimal(ratio_denominator) / Decimal(ratio_numerator)",
        selectors=[f"{U}/test_equity_backtest.py"],
        invariant="M2-G corporate-action conversion fills use the exact n/d share multiplier; value preservation is asserted by the ledger oracle",
    ),
    dict(
        id="M107-unavailable-slot-promotes",
        owner="test_unavailable_top_rank_stays_cash_without_promoting_lower_rank",
        file="src/tree_options/backtest/equity.py",
        anchor="if execution_bar is None:",
        replacement="if False:",
        selectors=[f"{U}/test_equity_backtest.py"],
        invariant="M2-G an unavailable top-quintile name keeps its slice in cash; promoting a lower-ranked name would silently change the registered strategy",
    ),
    # ---- M3 options era (plan §8, M108-M134) ------------------------------
    dict(
        id="M108-t1-receipt-shifted-same-day",
        owner="test_two_snapshots_with_correct_stamps",
        file="src/tree_options/synth_options/generate.py",
        anchor="return _wall(self._sessions[idx + 1], PUB_WALL)",
        replacement="return _wall(self._sessions[idx], PUB_WALL)",
        selectors=[f"{U}/test_synth_options_generate.py", f"{U}/test_data_options_surface.py"],
        invariant="M3-A option files publish 09:00 ET on the NEXT session (T+1); a same-day receipt leaks session-t facts to a close(t) decision",
    ),
    dict(
        id="M109-received-stamped-as-exchange",
        owner="test_quote_history_receipt_is_t1_publication",
        file="src/tree_options/synth_options/generate.py",
        anchor="received_timestamp=received,",
        replacement="received_timestamp=snap.exchange_timestamp,",
        selectors=[f"{U}/test_synth_options_generate.py"],
        invariant="M3-A quote receipt is the T+1 publication wall, never the intraday exchange stamp (receipt >= exchange)",
    ),
    dict(
        id="M110-eligible-window-unbounded",
        owner="test_eligibility_matches_independent_dollar_volume_oracle",
        file="src/tree_options/synth_options/generate.py",
        anchor="if len(window) > self.spec.eligibility_window_bars:\n                window.pop(0)",
        replacement="if False:\n                window.pop(0)",
        selectors=[f"{U}/test_synth_options_generate.py"],
        invariant="M3-A eligibility ranks on the bounded trailing 20-bar median dollar volume (the pre-declared 'eligible-set window' mutant: an unbounded window drifts the eligible set away from the independent oracle)",
    ),
    dict(
        id="M111-put-delta-sign",
        owner="test_delta_monotone_in_strike",
        file="src/tree_options/synth_options/greeks.py",
        anchor="return abs(norm_cdf(d1) - 1.0)",
        replacement="return abs(norm_cdf(d1))",
        selectors=[f"{U}/test_synth_options_generate.py"],
        invariant="M3-A put |delta| = |N(d1) - 1|; using the call delta breaks the strike-band selection and put-call parity",
    ),
    dict(
        id="M112-spread-halves-swapped",
        owner="test_put_call_parity_within_combined_spread",
        file="src/tree_options/synth_options/generate.py",
        anchor="ask = _tick_ceil(Decimal(repr(mid)) + Decimal(repr(half)))",
        replacement="ask = _tick_ceil(Decimal(repr(mid)) - Decimal(repr(half)))",
        selectors=[f"{U}/test_synth_options_generate.py"],
        invariant="M3-A the ask adds the half-spread, the bid subtracts it; swapping crosses the market",
    ),
    dict(
        id="M113-zero-bid-floor-removed",
        owner="test_zero_bid_tail_exists_and_never_negative",
        file="src/tree_options/synth_options/generate.py",
        anchor="bid = _tick_floor(Decimal(repr(mid)) - Decimal(repr(half)))",
        replacement="bid = _tick_ceil(Decimal(repr(mid)) - Decimal(repr(half)))",
        selectors=[f"{U}/test_synth_options_generate.py"],
        invariant="M3-A re-anchored (round 2): the defensive max(...,0)/never-locked guards are unreachable (the half-spread is proportional to mid); the LIVE zero-bid seam is the bid's tick-FLOOR rounding - sub-cent deep-wing bids quantize DOWN to 0.00, which is the bulk of the tail the gate's rejection criterion exercises",
    ),
    dict(
        id="M114-oi-plumbed-from-wrong-instant",
        owner="test_in_band_candidate_is_accepted_by_the_filter",
        file="src/tree_options/data/options_pit.py",
        anchor="open_interest=AsOf(value=entry.open_interest, available_at=received),",
        replacement=(
            "open_interest=AsOf(\n"
            "                    value=entry.open_interest,\n"
            "                    available_at=self._overlay.calendar.nth_after(decision_session, 1),\n"
            "                ),"
        ),
        selectors=[f"{U}/test_data_options_surface.py"],
        invariant="M3-B every candidate input is AsOf-wrapped at the file's receipt instant; OI stamped a session ahead is future-available and must go NOT_EVALUABLE",
    ),
    dict(
        id="M115-volume-applicability-excuses-missing",
        owner="test_contract_absent_from_file_lands_not_evaluable",
        file="src/tree_options/data/options_pit.py",
        anchor="same_day_volume=None,\n                same_day_volume_applicable=True,",
        replacement="same_day_volume=None,\n                same_day_volume_applicable=False,",
        selectors=[f"{U}/test_data_options_surface.py"],
        invariant="M3-B an absent contract's missing volume is NOT_EVALUABLE - the applicability flag must never excuse a missing input (filter F4)",
    ),
    dict(
        id="M116-settlement-pays-strike-side-swapped",
        owner="test_intrinsic_for_calls_and_puts",
        file="src/tree_options/options/settlement.py",
        anchor='return max(underlying - strike, Decimal("0"))',
        replacement='return max(strike - underlying, Decimal("0"))',
        selectors=[f"{U}/test_options_settlement.py"],
        invariant="M3-C a CALL settles at max(S - K, 0); the put-side intrinsic credits the wrong side",
    ),
    dict(
        id="M117-settlement-skips-lot-removal",
        owner="test_partial_settlement_then_sell",
        file="src/tree_options/ledger/book.py",
        anchor=(
            "remaining = settlement.quantity\n"
            '        cost_removed = Decimal("0")\n'
            "        while remaining > 0:\n"
            "            head = self._lots[settlement.contract_id][0]"
        ),
        replacement=(
            "remaining = 0\n"
            '        cost_removed = Decimal("0")\n'
            "        while remaining > 0:\n"
            "            head = self._lots[settlement.contract_id][0]"
        ),
        selectors=[f"{U}/test_options_settlement.py"],
        invariant="M3-C apply_settlement closes FIFO lots; leaving them open corrupts quantity and realized PnL",
    ),
    dict(
        id="M118-settlement-kind-swapped",
        owner="test_arm_b_expiry_settlements_close_positions",
        file="src/tree_options/backtest/options.py",
        anchor='elif kind == "expiry":\n                counters.expiries += 1',
        replacement='elif kind == "never":\n                counters.expiries += 1',
        selectors=[f"{U}/test_backtest_options.py"],
        invariant="M3-E expiry settlements are counted as expiries; miscounting them as terminals hides the machinery oracle",
    ),
    dict(
        id="M119-conservation-oracle-drops-settlements",
        owner="test_settlement_closes_lots_and_conserves",
        file="src/tree_options/ledger/book.py",
        anchor="cash += recomputed_cash",
        replacement='cash += Decimal("0")  # mutant: oracle drops settlement cash',
        selectors=[f"{U}/test_options_settlement.py"],
        invariant="M3-C the replay oracle independently recomputes and includes settlement cash; dropping it would certify a broken book",
    ),
    dict(
        id="M120-force-close-missed",
        owner="test_ratio_action_mid_hold_forces_close",
        file="src/tree_options/backtest/options.py",
        anchor='if classify_action(action.kind) != "ratio":',
        replacement="if True:",
        selectors=[f"{U}/test_backtest_options.py"],
        invariant="M3-E a ratio action announced mid-hold forces the position closed at the next window",
    ),
    dict(
        id="M121-execution-cancellation-dropped",
        owner="test_cancellations_window_and_toggle",
        file="src/tree_options/options/strategy.py",
        anchor="if order.decision_at < action.available_at <= execution_at:",
        replacement="if False:",
        selectors=[f"{U}/test_options_strategy.py"],
        invariant="M3-D orders whose underlying had an action announced overnight are cancelled at execution, never filled blind",
    ),
    dict(
        id="M122-exit-same-session",
        owner="test_arm_a_round_trips_sell_in_four_sessions",
        file="src/tree_options/backtest/options.py",
        anchor="exit_session = calendar.nth_after(session, config.exit_sessions_after_entry)",
        replacement="exit_session = session",
        selectors=[f"{U}/test_backtest_options.py"],
        invariant="M3-E arm A exits on the 4th session after entry at the 10:00 window - never same-session",
    ),
    dict(
        id="M123-mark-at-ask",
        owner="test_mark_uses_prior_file_eod_bid",
        file="src/tree_options/backtest/options.py",
        anchor="market_value += (entry.quote_eod.bid * ledger.quantity(contract_id) * 100).quantize(",
        replacement="market_value += (entry.quote_eod.ask * ledger.quantity(contract_id) * 100).quantize(",
        selectors=[f"{U}/test_backtest_options.py"],
        invariant="M3-E open positions are marked at the strictly-knowable file(t-1) EOD BID, never the ask",
    ),
    dict(
        id="M124-inv11-fraction-inverted",
        owner="test_improvement_on_half_tick_mid_rounds_conservatively",
        file="src/tree_options/guards/fills.py",
        anchor="ticks = math.ceil(exact / 2)  # conservative: round the BUY price UP",
        replacement="ticks = math.floor(exact / 2)  # mutant: buy rounds down",
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="INV-11 buy-side improvement rounds UP (conservative for the taker), never down",
    ),
    dict(
        id="M125-sizing-ignores-fees",
        owner="test_affordable_contracts_fee_inclusive",
        file="src/tree_options/options/strategy.py",
        anchor="while estimate > 0 and per_contract * estimate + fee_model.order_fees(estimate) > budget:",
        replacement="while estimate > 0 and per_contract * estimate > budget:",
        selectors=[f"{U}/test_options_strategy.py"],
        invariant="M3-D whole-contract sizing includes per-contract fees; ignoring them overspends the budget",
    ),
    dict(
        id="M126-quintile-over-full-universe",
        owner="test_build_candidates_direction_and_bands",
        file="src/tree_options/options/strategy.py",
        anchor="count = max(1, math.ceil(len(rows) / 5))",
        replacement="count = len(rows)",
        selectors=[f"{U}/test_options_strategy.py"],
        invariant="M3-D the cut is quintiles (top -> calls, bottom -> puts), never the full cross-section",
    ),
    dict(
        id="M127-dte-in-sessions",
        owner="test_pick_expiry_uses_calendar_days",
        file="src/tree_options/options/strategy.py",
        anchor="return min(in_band, key=lambda e: (abs((e - decision_session).days - config.target_dte), e))",
        replacement=(
            "return min(\n"
            "            in_band,\n"
            "            key=lambda e: (\n"
            "                abs(\n"
            "                    (surface.overlay.calendar.ordinal(e)\n"
            "                     - surface.overlay.calendar.ordinal(decision_session))\n"
            "                    - config.target_dte\n"
            "                ),\n"
            "                e,\n"
            "            ),\n"
            "        )"
        ),
        selectors=[f"{U}/test_options_strategy.py"],
        invariant="M3-D the expiry pick targets 45 CALENDAR days' DTE (re-anchored round 2 to the nearest-target key itself — widening only the band was dominated by the days-based tie-break and semantically equivalent)",
    ),
    dict(
        id="M128-future-file-visible",
        owner="test_file_visible_exactly_from_receipt",
        file="src/tree_options/data/options_pit.py",
        anchor="if self._overlay.publication_of(session) <= as_of:",
        replacement="if True:",
        selectors=[f"{U}/test_data_options_surface.py"],
        invariant="M3-B a file is visible exactly from its receipt instant; an always-visible file leaks the future chain",
    ),
    dict(
        id="M129-dead-contract-tradable",
        owner="test_fill_after_expiration_rejected_itm_fixture",
        file="src/tree_options/guards/fills.py",
        anchor="if not contract.exists_on(execution_session):",
        replacement="if False:",
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="M3 a dead (expired) contract must never trade; re-anchored (round 2) to the listing-window guard - for standard contracts (listing_end == expiration) it is the guard that actually fires; the explicit expired_on check is unreachable behind it (the M0 test now pins the specific rejection code)",
    ),
    dict(
        id="M130-spans-earnings-fed-none",
        owner="test_candidate_snapshot_carries_file_receipt_and_truthful_earnings",
        file="src/tree_options/data/options_pit.py",
        anchor=(
            "ask=AsOf(value=entry.quote_eod.ask, available_at=received),\n"
            "            underlying_20d_median_dollar_volume=AsOf(value=dollar_volume, available_at=received),\n"
            "            spans_earnings=AsOf(value=False, available_at=received),"
        ),
        replacement=(
            "ask=AsOf(value=entry.quote_eod.ask, available_at=received),\n"
            "            underlying_20d_median_dollar_volume=AsOf(value=dollar_volume, available_at=received),\n"
            "            spans_earnings=None,"
        ),
        selectors=[f"{U}/test_data_options_surface.py", f"{U}/test_backtest_options.py"],
        invariant="M3-B spans_earnings is fed AsOf(False, receipt) - the worlds contain no earnings; None collapses every candidate to NOT_EVALUABLE (empty backtest)",
    ),
    dict(
        id="M131-election-visibility-extends-to-close",
        owner="test_election_window_is_ten_oclock_visibility",
        file="src/tree_options/backtest/options.py",
        anchor="visible_by=instant,",
        replacement="visible_by=calendar.session_close(session),",
        selectors=[f"{U}/test_backtest_options.py"],
        invariant="M3-E the election consumes only actions visible by the 10:00 window; extending visibility to the close leaks same-evening announcements",
    ),
    dict(
        id="M132-exercise-ignores-style-guard",
        owner="test_early_exercise_of_european_refused",
        file="src/tree_options/options/settlement.py",
        anchor='if contract.exercise_style != "american":',
        replacement="if False:",
        selectors=[f"{U}/test_options_settlement.py"],
        invariant="M3-C european contracts have no early-exercise right; the mint must refuse",
    ),
    dict(
        id="M133-settlement-priced-at-strike-not-close",
        owner="test_settlement_closes_lots_and_conserves",
        file="src/tree_options/options/settlement.py",
        anchor="settlement_price=reference_bar.close,",
        replacement="settlement_price=contract.strike,",
        selectors=[f"{U}/test_options_settlement.py"],
        invariant="M3-C the settlement strikes at the reference bar's CLOSE; recording the strike breaks the oracle's independent cash recomputation",
    ),
    dict(
        id="M134-election-ignores-dividend-branch",
        owner="test_dividend_branch_elects_for_calls",
        file="src/tree_options/options/exercise.py",
        anchor='inputs.call_put == "C"\n        and inputs.pending_dividend_per_share is not None',
        replacement="False\n        and inputs.pending_dividend_per_share is not None",
        selectors=[f"{U}/test_options_settlement.py"],
        invariant="M3-C branch (a): a call elects when the visible dividend dominates the file(t-1) time value",
    ),
    # ---- M4 real-data adapter era (integration, M135-M142) -----------------
    dict(
        id="M135-publication-wall-same-session",
        owner="test_t_plus_1_wall_friday_publishes_monday",
        file="src/tree_options/data/cboe_eod.py",
        anchor="local = datetime.combine(next_trading_session(session), PUB_WALL, tzinfo=SESSION_TIMEZONE)",
        replacement="local = datetime.combine(session, PUB_WALL, tzinfo=SESSION_TIMEZONE)",
        selectors=[f"{U}/test_cboe_eod.py"],
        invariant=(
            "M4-A session t's file publishes 09:00 ET on the NEXT weekend-skipping"
            " session (T+1); a same-day wall either leaks session-t facts to a"
            " close(t) decision or trips the parse-time PIT check"
        ),
    ),
    dict(
        id="M136-pit-violation-accepted",
        owner="test_overlay_rejects_pit_violation",
        file="src/tree_options/data/cboe_eod.py",
        anchor="if snap.exchange_timestamp > file.received_at:",
        replacement="if False:",
        selectors=[f"{U}/test_real_overlay.py"],
        invariant=(
            "M4-A every snapshot must satisfy exchange_timestamp <= received_timestamp"
            " (validate_pit_invariants runs at parse time AND overlay construction);"
            " a future-exchanged quote accepted into the overlay is a leak"
        ),
    ),
    dict(
        id="M137-no-cgi-index-zeros-ingested",
        owner="test_no_cgi_index_refusal_names_symbol",
        file="src/tree_options/data/cboe_eod.py",
        anchor='if variant == "no_cgi" and symbol.startswith("^"):',
        replacement="if False:",
        selectors=[f"{U}/test_cboe_eod.py"],
        invariant=(
            "M4-A a no_cgi file zeroes index underlyings' quotes: the parse refuses"
            " naming the symbol; only the degenerate-quote backstop would remain"
            " if the license check were gutted"
        ),
    ),
    dict(
        id="M138-duplicate-row-overwritten",
        owner="test_duplicate_contract_refused_never_overwritten",
        file="src/tree_options/data/cboe_eod.py",
        anchor="if contract_id in session_seen:",
        replacement="if False:",
        selectors=[f"{U}/test_cboe_eod.py"],
        invariant=(
            "M4-A a second row for the same (session, contract) is refused and"
            " counted (first row kept); ingesting both double-counts chains and OI"
        ),
    ),
    dict(
        id="M139-underlying-pair-swapped",
        owner="test_file_level_underlying_prefers_1545_pair",
        file="src/tree_options/data/cboe_eod.py",
        anchor="return bid, ask",
        replacement="return ask, bid",
        selectors=[f"{U}/test_cboe_eod.py"],
        invariant=(
            "M4-A the file-level underlying pair keeps its (bid, ask) order; a"
            " swapped pair inverts every spot mid consumer and the crossed check"
        ),
    ),
    dict(
        id="M140-abs-delta-sign-flipped",
        owner="test_normal_row_mapping_and_decimal_exactness",
        file="src/tree_options/data/cboe_eod.py",
        anchor="abs_delta = delta.copy_abs()",
        replacement="abs_delta = -delta.copy_abs()",
        selectors=[f"{U}/test_cboe_eod.py"],
        invariant=(
            "M4-A abs_delta enters the schema as |delta| (ge=0); a sign flip either"
            " refuses every row at the schema gate or corrupts the ATM band inputs"
        ),
    ),
    dict(
        id="M141-delivery-code-forced-nonempty",
        owner="test_empty_delivery_code_is_the_norm",
        file="src/tree_options/data/cboe_eod.py",
        anchor="if delivery_code:",
        replacement="if True:",
        selectors=[f"{U}/test_cboe_eod.py"],
        invariant=(
            "M4-A an EMPTY delivery_code is the standard deliverable (the trailing"
            " empty field is the norm); treating every row as nonstandard refuses"
            " the whole file and destroys coverage"
        ),
    ),
    dict(
        id="M142-early-close-1545-forced-present",
        owner="test_calendar_protocol",
        file="src/tree_options/data/real_overlay.py",
        anchor="if file.entries and all(e.quote_1545 is None for e in file.entries)",
        replacement="if False",
        selectors=[f"{U}/test_real_overlay.py"],
        invariant=(
            "M4-A an early close is detected from the files (every entry lacks the"
            " 15:45 snapshot) and the calendar closes the session at 13:00 ET;"
            " pretending the 1545 snapshot is present stamps decisions past the"
            " real close"
        ),
    ),
    # ---- M4-B hardening (client/adapter/manifest/inspector, M143-M156) ------
    dict(
        id="M143-parse-float-float",
        owner="test_loads_exact_builds_decimals_from_raw_text",
        file="src/tree_options/data/massive_client.py",
        anchor="json.loads(text, parse_float=Decimal)",
        replacement="json.loads(text, parse_float=float)",
        selectors=[f"{U}/test_massive_client.py"],
        invariant=(
            "M4-B loads_exact hands every fractional JSON token to Decimal as the"
            " vendor's RAW SOURCE TEXT; parsing through float rebuilds prices from"
            " a binary approximation and launders it into every price field"
        ),
    ),
    dict(
        id="M144-not-authorized-gutted",
        owner="test_not_authorized_arrives_as_http_200_and_still_raises",
        file="src/tree_options/data/massive_client.py",
        anchor="if status == NOT_AUTHORIZED_STATUS:",
        replacement="if False:",
        selectors=[f"{U}/test_massive_client.py"],
        invariant=(
            "M4-B NOT_AUTHORIZED arrives as HTTP 200 in the BODY; gutting the"
            " status check reads a tier refusal as a generic error instead of the"
            " terminal MassiveNotEntitledError echoing the vendor's message"
        ),
    ),
    dict(
        id="M145-foreign-host-accepted",
        owner="test_foreign_next_url_host_is_refused_before_the_key_is_sent",
        file="src/tree_options/data/massive_client.py",
        anchor="if parts.netloc and parts.netloc != self._host:",
        replacement="if False:",
        selectors=[f"{U}/test_massive_client.py"],
        invariant=(
            "M4-B a next_url on a foreign host is refused BEFORE the key is"
            " appended to it; accepting one hands the API key to someone else's"
            " server"
        ),
    ),
    dict(
        id="M146-cache-write-skips-redaction",
        owner="test_cached_bytes_and_filenames_are_key_free",
        file="src/tree_options/data/massive_client.py",
        anchor="staging.write_bytes(redact_bytes(body, secret))",
        replacement="staging.write_bytes(body)",
        selectors=[f"{U}/test_massive_client.py"],
        invariant=(
            "M4-B cached bodies are stored key-redacted; writing the raw bytes"
            " persists the secret to disk, where the cache outlives the process"
        ),
    ),
    dict(
        id="M147-cache-self-heal-gutted",
        owner="test_corrupt_cache_entry_self_heals_from_the_wire",
        file="src/tree_options/data/massive_client.py",
        anchor="except MassiveApiError:",
        replacement="except AssertionError:",
        selectors=[f"{U}/test_massive_client.py"],
        invariant=(
            "M4-B an undecodable cache entry self-heals (discard, re-account as a"
            " miss, refetch from the wire); letting the decode failure escape"
            " makes one torn write raise forever instead of repairing the entry"
            " (the branch has no `if` to void, so the mutant narrows the catch"
            " until the refusal escapes - the same corruption re-raises)"
        ),
    ),
    dict(
        id="M148-auth-rejected-swallowed",
        owner="test_401_and_403_are_terminal_auth_rejections",
        file="src/tree_options/data/massive_client.py",
        anchor="if status in AUTH_REJECTED_HTTP_STATUSES:",
        replacement="if False:",
        selectors=[f"{U}/test_massive_client.py"],
        invariant=(
            "M4-B HTTP 401/403 is a TERMINAL MassiveAuthRejectedError - never"
            " retried, never cached; swallowing the branch degrades a dead key"
            " into a generic MassiveApiError and loses the rotate-the-key pin"
        ),
    ),
    dict(
        id="M149-server-5xx-not-retried",
        owner="test_503_is_retried_with_backoff_then_succeeds",
        file="src/tree_options/data/massive_client.py",
        anchor="RETRYABLE_HTTP_STATUSES = frozenset({429, 502, 503, 504})",
        replacement="RETRYABLE_HTTP_STATUSES = frozenset({429})",
        selectors=[f"{U}/test_massive_client.py"],
        invariant=(
            "M4-B the vendor edge statuses 502/503/504 share the bounded backoff"
            " cadence with 429; unclassifying them makes every edge blip a"
            " terminal MassiveApiError instead of one retried request (mutant"
            " re-pinned from a bare `if False` to the classification set itself:"
            " the set IS the retry decision, and carving out only 5xx leaves the"
            " 429 semantics untouched)"
        ),
    ),
    dict(
        id="M150-transport-not-retried",
        owner="test_transport_failure_is_retried_then_raises_with_attempts",
        file="src/tree_options/data/massive_client.py",
        anchor=(
            "            except Exception as exc:  # redact before wrapping: exc may echo the URL\n"
            "                transport_failure = MassiveTransportError(\n"
            '                    f"{endpoint}: {redact(str(exc), self._api_key)}"\n'
            "                )\n"
            "                failed_transport = True"
        ),
        replacement=(
            "            except Exception as exc:  # redact before wrapping: exc may echo the URL\n"
            "                raise"
        ),
        selectors=[f"{U}/test_massive_client.py"],
        invariant=(
            "M4-B a transport-level failure is wrapped key-redacted and retried"
            " on the one bounded cadence, raising with the attempt count when"
            " exhausted; re-raising on first sight turns a blip into an immediate"
            " unaccounted failure (anchored at the generic wrap the owner's"
            " RuntimeError-raising transport exercises; the MassiveTransportError"
            " passthrough above feeds the same retry loop)"
        ),
    ),
    dict(
        id="M151-nonstandard-spc-blind",
        owner="test_nonstandard_deliverable_is_kept_and_flagged",
        file="src/tree_options/data/massive_options.py",
        anchor="return self.shares_per_contract == STANDARD_SHARES_PER_CONTRACT",
        replacement="return True",
        selectors=[f"{U}/test_massive_options.py"],
        invariant=(
            "M4-B a shares_per_contract != 100 contract is KEPT but FLAGGED - the"
            " adjusted/non-standard deliverable tell this lane exists to surface;"
            " answering standard for everything pools adjusted contracts with"
            " textbook ones"
        ),
    ),
    dict(
        id="M152-float-branch-gutted",
        owner="test_float_decoded_body_is_refused_not_laundered",
        file="src/tree_options/data/massive_options.py",
        anchor="if isinstance(value, float):",
        replacement="if False:",
        selectors=[f"{U}/test_massive_options.py"],
        invariant=(
            "M4-B _as_decimal refuses a float outright - seeing one means the body"
            " was decoded with plain json.loads and exactness is already gone,"
            " and the refusal names loads_exact so the cause is discoverable"
            " (the generic not-a-number fallback the mutant falls through to"
            " hides it)"
        ),
    ),
    dict(
        id="M153-manifest-hash-void",
        owner="test_verify_detects_a_tampered_capture_file",
        file="src/tree_options/data/massive_manifest.py",
        anchor="if sha256_hex(raw) != entry.sha256:",
        replacement="if False:",
        selectors=[f"{U}/test_massive_manifest.py"],
        invariant=(
            "M4-B verify re-hashes every listed capture file from its raw bytes"
            " and refuses on mismatch - the build-side hash alone cannot detect"
            " post-build tampering; voiding the comparison lets an edited capture"
            " file pass as provenance-clean (mutant re-pinned to the VERIFY-side"
            " hash: build and verify hash independently, so voiding the build"
            " side alone would not model the leak)"
        ),
    ),
    dict(
        id="M154-capture-complete-inverted",
        owner="test_incomplete_capture_and_schema_drift_are_reported",
        file="scripts/inspect_structural_coverage.py",
        anchor='return pages, not pages[-1].get("next_url")',
        replacement="return pages, True",
        selectors=[f"{U}/test_inspect_structural_coverage.py"],
        invariant=(
            "M4-B a page chain whose LAST page still carries next_url is an"
            " INCOMPLETE capture - reported, never silently under-counted;"
            " declaring every chain complete hides a short master that"
            " misdescribes the contract universe"
        ),
    ),
    dict(
        id="M155-spc-restatement-blind",
        owner="test_adjustment_timeline_is_first_class",
        file="scripts/inspect_structural_coverage.py",
        anchor="if before != after:",
        replacement="if False:",
        selectors=[f"{U}/test_inspect_structural_coverage.py"],
        invariant=(
            "M4-B a shares_per_contract restatement under a stable ticker is THE"
            " adjusted-chain event; blinding the before != after check drops it"
            " from the adjustment timeline, the risk the lane exists to surface"
        ),
    ),
    dict(
        id="M156-statusless-page-accepted",
        owner="test_a_page_without_a_status_field_refuses",
        file="scripts/inspect_structural_coverage.py",
        anchor="if status is None:",
        replacement="if False:",
        selectors=[f"{U}/test_inspect_structural_coverage.py"],
        invariant=(
            "M4-B a body with NO status field is not a vendor body; the named"
            " fail-closed refusal (mirroring WS-D1's client) is what the owner"
            " pins - the generic status-not-OK fallback the mutant falls through"
            " to would make this the softer reader of the same capture"
        ),
    ),
    dict(
        id="M157-budget-precharge-shrunk",
        owner="test_the_budget_precharges_the_worst_case_and_refunds",
        file="scripts/capture_massive_structural.py",
        anchor="budget.charge_block(what, blocks=client.backoff.max_attempts)",
        replacement="budget.charge_block(what, blocks=1)",
        selectors=[f"{U}/test_capture_massive_structural.py"],
        invariant=(
            "M4-B the budget must reserve each wire call's WORST case"
            " (max_attempts) before the request leaves; charging one block"
            " lets an unaffordable call touch the wire (owner asserts"
            " vendor.calls == 0 under a limit smaller than max_attempts)"
        ),
    ),
    dict(
        id="M158-nothing-captured-exits-zero",
        owner="test_a_fully_failed_run_exits_nonzero",
        file="scripts/capture_massive_structural.py",
        anchor=(
            'if not (any(m["pages"] for m in masters) or manifest["bars"]'
            ' or manifest["spot_proxy"]):'
        ),
        replacement="if False:",
        selectors=[f"{U}/test_capture_massive_structural.py"],
        invariant=(
            "M4-B a run that captured nothing must say so with exit 4, not"
            " report success; voiding the nothing-captured branch turns a"
            " dead sweep (every master errored, zero pages) into exit 0"
        ),
    ),
    # ---- M4-C hardening (derived greeks/overlay, monthlies, atm-grid, M159-M170)
    dict(
        id="M159-bisection-bracket-inverted",
        owner="test_implied_vol_round_trips_across_moneyness",
        file="src/tree_options/data/massive_derived.py",
        anchor=("        if price_mid < premium:\n            lo = mid"),
        replacement=("        if price_mid < premium:\n            hi = mid"),
        selectors=[f"{U}/test_massive_derived.py"],
        invariant=(
            "M4-C the bisection bracket must TIGHTEN toward the premium: an"
            " underpricing midpoint moves lo up (bs_price is monotone in iv);"
            " moving hi down instead collapses the bracket onto the lo edge,"
            " every later midpoint re-underprices, and the solve can never"
            " land within tol - it dies in the loud did-not-converge refusal"
            " instead of round-tripping the planted iv"
        ),
    ),
    dict(
        id="M160-under-intrinsic-accepted",
        owner="test_refuses_premium_below_the_lower_bound",
        file="src/tree_options/data/massive_derived.py",
        anchor="if premium < price_lo:",
        replacement="if False:",
        selectors=[f"{U}/test_massive_derived.py"],
        invariant=(
            "M4-C a premium below the nearly-zero-vol price is arbitrage or"
            " degenerate data - the named lower-bound refusal, never a silent"
            " clamp and never a doomed loop; voiding the guard sends the"
            " solver into a guaranteed 200-iteration walk that ends in a"
            " message naming no bound (every in-bracket price exceeds the"
            " premium, so tol is never met)"
        ),
    ),
    dict(
        id="M161-over-bracket-accepted",
        owner="test_refuses_premium_above_the_upper_bound",
        file="src/tree_options/data/massive_derived.py",
        anchor="if premium > price_hi:",
        replacement="if False:",
        selectors=[f"{U}/test_massive_derived.py"],
        invariant=(
            "M4-C a premium above bs_price at the hi bracket edge cannot be"
            " priced by the bracket at all - the named upper-bound refusal"
            " says so before any iteration; voiding the guard spends the"
            " whole iteration budget walking to hi and fails with a message"
            " that names neither the bound nor the numbers"
        ),
    ),
    dict(
        id="M162-delta-at-wrong-iv",
        owner="test_implied_vol_round_trips_across_moneyness",
        file="src/tree_options/data/massive_derived.py",
        anchor="iv=iv,",
        replacement="iv=1e-4,",
        selectors=[f"{U}/test_massive_derived.py"],
        invariant=(
            "M4-C derived_abs_delta must evaluate |delta| at the SOLVED iv;"
            " pricing the greek at the bracket's lo edge instead saturates"
            " N(d1) to exactly 0.0 or 1.0 at every moneyness, killing the"
            " owner's strict (0, 1) sanity band - a derived greek that no"
            " longer co-moves with the premium it was inverted from"
        ),
    ),
    dict(
        id="M163-stale-bar-carried-forward",
        owner="test_stale_bar_never_carries_forward",
        file="src/tree_options/data/massive_overlay.py",
        anchor="if not fresh:",
        replacement="if False:",
        selectors=[f"{U}/test_massive_overlay.py"],
        invariant=(
            "M4-C a bar more than staleness_sessions behind the capture"
            " frontier is NOT_EVALUABLE 'stale' - a stale VWAP is never"
            " carried forward as the session's quote; voiding the freshness"
            " branch derives six-sessions-old evidence as if it were today's"
            " (the stale cell flips to DERIVED while its premium stays)"
        ),
    ),
    dict(
        id="M164-zero-volume-derived",
        owner="test_zero_volume_bar_refuses",
        file="src/tree_options/data/massive_overlay.py",
        anchor="if bar.volume == 0:",
        replacement="if False:",
        selectors=[f"{U}/test_massive_overlay.py"],
        invariant=(
            "M4-C a zero-volume bar carried no trades, so its VWAP token is"
            " not a trade price - refused by name, never derived; voiding"
            " the guard launders a placeholder VWAP into an iv/delta pair"
            " that looks like evidence (the owner's zero-volume cell derives)"
        ),
    ),
    dict(
        id="M165-refusal-census-hidden",
        owner="test_derivation_refusal_is_counted_not_fatal",
        file="src/tree_options/data/massive_overlay.py",
        anchor="not_evaluable_refused=refused_count,",
        replacement="not_evaluable_refused=0,",
        selectors=[f"{U}/test_massive_overlay.py"],
        invariant=(
            "M4-C a derivation refusal is NOT fatal but MUST be censused: the"
            " cell is recorded NOT_EVALUABLE with its reason and counted in"
            " derived_stats() for the evidence packet; zeroing the feed"
            " loads the overlay clean while hiding every refusal. (Anchored"
            " at the count feed, not a bare re-raise in the except branch:"
            " a re-raise fails the overlay fixture at SETUP, which pytest"
            " reports as ERROR - not a FAILED line - so it is not a"
            " behavioral kill; the count is the observable the owner pins)"
        ),
    ),
    dict(
        id="M166-received-wall-collapsed",
        owner="test_pit_invariants_on_every_row",
        file="src/tree_options/data/massive_overlay.py",
        anchor="received = publication_instant(session)",
        replacement="received = session_close_instant(session)",
        selectors=[f"{U}/test_massive_overlay.py"],
        invariant=(
            "M4-C PIT receipt is the shared T+1 09:00 ET wall"
            " (cboe_eod.publication_instant): session t's bar is usable only"
            " from t+1 morning; stamping receipt at the same session's"
            " 16:00 close makes session t's own bar decidable at close(t) -"
            " exactly the lookahead the wall exists to prevent (equal"
            " stamps still satisfy exchange <= received, so the rows load"
            " and only the per-row wall assertion dies)"
        ),
    ),
    dict(
        id="M167-float-boundary-leak",
        owner="test_derived_quote_matches_the_pinned_derivation_surface",
        file="src/tree_options/data/massive_overlay.py",
        anchor="iv=Decimal(repr(iv))",
        replacement="iv=iv",
        selectors=[f"{U}/test_massive_overlay.py"],
        invariant=(
            "M4-C the sanctioned float island ends at the record boundary:"
            " the solver's float outputs are pinned once to Decimal(repr(x))"
            " so no binary approximation reaches an exact field; passing the"
            " float through leaves iv a float whose value differs from"
            " Decimal(repr(iv)), and the owner's pinned-surface equality"
            " catches it. (Decimal(str(x)) would be a no-op mutant: repr and"
            " str are identical for floats in py3)"
        ),
    ),
    dict(
        id="M168-monthly-window-floorless",
        owner="test_a_full_year_has_exactly_one_monthly_expiry_per_month",
        file="src/tree_options/time/monthlies.py",
        anchor="return is_friday(d) and 15 <= d.day <= 21",
        replacement="return is_friday(d) and d.day <= 21",
        selectors=[f"{U}/test_monthlies.py"],
        invariant=(
            "M4-C the monthly rule is THE THIRD Friday - the unique Friday"
            " in days 15-21; dropping the floor lets the first and second"
            " Fridays count too (~three 'monthlies' per month), destroying"
            " the exactly-one-expiry-per-month invariant the atm-grid"
            " monthly filter and every monthly label rests on"
        ),
    ),
    dict(
        id="M169-atm-grid-ladder-order",
        owner="test_atm_grid_takes_atm_plus_minus_band_distinct_strikes_by_rank",
        file="scripts/capture_massive_structural.py",
        anchor="ranked = sorted(by_strike, key=lambda s: (abs(s - anchor), s))",
        replacement="ranked = sorted(by_strike, key=lambda s: s)",
        selectors=[f"{U}/test_capture_massive_structural.py"],
        invariant=(
            "M4-C the band is a RANK in the ladder by |strike - spot| (ties"
            " by strike), never a plain ladder order: sorting by strike"
            " alone takes the lowest 2*band+1 strikes regardless of where"
            " spot sits, so the 'at-the-money' grid describes the bottom of"
            " the ladder, not the money (spot no longer moves the grid)"
        ),
    ),
    dict(
        id="M170-atm-grid-dedup-void",
        owner="test_atm_grid_dedups_a_contract_chosen_at_two_as_ofs",
        file="scripts/capture_massive_structural.py",
        anchor=("                    if ticker in seen:\n                        duplicates += 1"),
        replacement=("                    if False:\n                        duplicates += 1"),
        selectors=[f"{U}/test_capture_massive_structural.py"],
        invariant=(
            "M4-C a contract's bar series is fetched ONCE per run (per"
            " contract life) - the lane's stated cost model; voiding the"
            " run-wide dedup pays for the same ticker at every as_of it is"
            " in-band and drops the dedup note, silently multiplying wire"
            " spend. (The anchor carries the duplicates counter because the"
            " bare `if ticker in seen:` line also matches the"
            " representative chooser's dedup block)"
        ),
    ),
    dict(
        id="M171-vwap-participation-cap-gutted",
        owner="test_participation_cap_bars_beyond_volume",
        file="src/tree_options/guards/fills.py",
        anchor="capacity = math.floor(self.fill_size_fraction * vq.volume)",
        replacement="capacity = order.quantity",
        selectors=[f"{G}/test_fill_vwap.py"],
        invariant=(
            "G3 a vwap fill's quantity is capped by the session's OBSERVED"
            " volume (participation, not invention): gutting the cap lets an"
            " order fill its full quantity against a session that traded"
            " fewer contracts than the fill claims"
        ),
    ),
    dict(
        id="M172-vwap-zero-volume-accepted",
        owner="test_zero_volume_session_unfillable_not_fabricated",
        file="src/tree_options/schemas/market.py",
        anchor=("    if q.volume < 1:\n        raise ZeroVolumeVwapError("),
        replacement=("    if False:\n        raise ZeroVolumeVwapError("),
        selectors=[f"{G}/test_fill_vwap.py"],
        invariant=(
            "G3 a zero-volume session has no executions to participate in:"
            " the door must refuse (unfillable), never pass a volume-less"
            " bar to an engine that would then price a fabricated VWAP fill"
        ),
    ),
    dict(
        id="M173-vwap-tick-rounding-flipped",
        owner="test_buy_fills_at_vwap_rounded_up",
        file="src/tree_options/schemas/market.py",
        anchor='return (cents.to_integral_value(rounding="ROUND_CEILING") / 100).quantize(',
        replacement='return (cents.to_integral_value(rounding="ROUND_FLOOR") / 100).quantize(',
        selectors=[f"{G}/test_fill_vwap.py"],
        invariant=(
            "G3 the sub-tick VWAP rounds AGAINST the taker (buy UP):"
            " quantization may only worsen the fill. Rounding a buy DOWN"
            " gifts the taker a better-than-benchmark executable"
        ),
    ),
    dict(
        id="M174-delta-provenance-gate-gutted",
        owner="test_unaccepted_provenance_is_not_evaluable",
        file="src/tree_options/candidates/filters.py",
        anchor="elif snap.abs_delta.provenance not in self.accepted_delta_provenance:",
        replacement="elif False:",
        selectors=[f"{U}/test_candidate_volume_flow.py"],
        invariant=(
            "G3 an abs_delta whose provenance the protocol does not accept"
            " is NOT_EVALUABLE: gutting the gate lets any stamped-or-stale"
            " provenance flow into the band check as if the protocol had"
            " ratified it"
        ),
    ),
    dict(
        id="M175-oi-drop-undisclosed",
        owner="test_open_interest_and_spread_dropped_with_disclosure",
        file="src/tree_options/candidates/filters.py",
        # elif since the review hardening: a SUPPLIED-OI incoherence branch
        # now precedes the disclosure branch.
        anchor=(
            '        elif self.liquidity_regime == "volume_flow":\n'
            "            results.append(\n"
            "                RuleResult(\n"
            '                    "open_interest",'
        ),
        replacement=(
            "        elif False:\n"
            "            results.append(\n"
            "                RuleResult(\n"
            '                    "open_interest",'
        ),
        selectors=[f"{U}/test_candidate_volume_flow.py"],
        invariant=(
            "G3 the open-interest drop is DISCLOSED, not silent: without the"
            " regime branch a tier with no OI records NOT_EVALUABLE and"
            " rejects every candidate - the audit must say NOT_APPLICABLE"
            " naming the absent input"
        ),
    ),
    dict(
        id="M176-vwap-participation-ledger-gutted",
        owner="test_second_order_cannot_reuse_vwap_bar_capacity",
        file="src/tree_options/guards/fills.py",
        anchor=(
            "capacity = math.floor(self.fill_size_fraction * vq.volume) - already_participated"
        ),
        replacement=("capacity = math.floor(self.fill_size_fraction * vq.volume) - 0"),
        selectors=[f"{G}/test_fill_vwap.py"],
        invariant=(
            "G3 (review P0-2) participation is CUMULATIVE per (contract, bar"
            " session): zeroing the ledger lets a second order - or a"
            " partial-sequence - mint fills beyond the session's entire"
            " observed volume against the same bar"
        ),
    ),
    dict(
        id="M177-vwap-session-stamp-unchecked",
        owner="test_mislabeled_bar_session_refuses",
        file="src/tree_options/guards/fills.py",
        anchor=("if self.calendar.session_close(vq.quote.session) != vq.quote.exchange_timestamp:"),
        replacement="if False:",
        selectors=[f"{G}/test_fill_vwap.py"],
        invariant=(
            "G3 (review P0-1) a bar's session label must be coherent with"
            " its own exchange stamp (the close of that session): without"
            " the check a bar LABELED one session but stamped at another's"
            " close fills as if it were the labeled session"
        ),
    ),
    dict(
        id="M179-vwap-bar-recency-gutted",
        owner="test_week_old_coherent_bar_refuses",
        file="src/tree_options/guards/fills.py",
        anchor="if exec_ord - bar_ordinal != 1:",
        replacement="if False:",
        selectors=[f"{G}/test_fill_vwap.py"],
        invariant=(
            "G3 (review r2) a vwap fill may consume ONLY the session"
            " immediately before the execution session: an older coherent"
            " bar is the last observed reality merely because intervening"
            " sessions traded nothing, and filling at its VWAP fabricates"
            " liquidity those zero-volume sessions deny"
        ),
    ),
    dict(
        id="M178-float-vwap-laundered",
        owner="test_float_vwap_refused_at_the_boundary",
        file="src/tree_options/schemas/market.py",
        anchor=(
            "        if isinstance(v, float):\n"
            '            raise ValueError(f"vwap must be Decimal, got float {v!r}")'
        ),
        replacement=(
            "        if False:\n"
            '            raise ValueError(f"vwap must be Decimal, got float {v!r}")'
        ),
        selectors=[f"{G}/test_fill_vwap.py"],
        invariant=(
            "G3 (review P0-6) a float vwap means exactness was lost"
            " upstream; pydantic's lax float->Decimal coercion runs before"
            " after-validators, so only a before-mode gate refuses it at"
            " the boundary instead of laundering a binary approximation"
            " into a price"
        ),
    ),
    dict(
        id="M180-lifecycle-whitelist-gutted",
        owner="test_full_matrix_refuses_everything_else",
        file="src/tree_options/runstate/states.py",
        anchor=("    if target not in LEGAL_EDGES.get(source, frozenset()):\n        return False"),
        replacement="    if False:\n        return False",
        selectors=[f"{U}/test_runstate_lifecycle.py"],
        invariant=(
            "A1 lifecycle legality is an explicit whitelist; gutting the"
            " membership check legalizes every 16x16 pair, and the full-grid"
            " owner test pins every refused pair"
        ),
    ),
    dict(
        id="M181-failed-retry-reaches-sealed",
        owner="test_failed_cannot_reach_sealed_lane",
        file="src/tree_options/runstate/states.py",
        anchor=(
            "    RunState.FAILED: frozenset(\n"
            "        {RunState.CAPTURING, RunState.INSPECTION_RUNNING, RunState.BARS_CAPTURING}\n"
            "    ),"
        ),
        replacement=(
            "    RunState.FAILED: frozenset(\n"
            "        {\n"
            "            RunState.CAPTURING,\n"
            "            RunState.INSPECTION_RUNNING,\n"
            "            RunState.BARS_CAPTURING,\n"
            "            RunState.SEALED_RUNNING,\n"
            "        }\n"
            "    ),"
        ),
        selectors=[f"{U}/test_runstate_lifecycle.py"],
        invariant=(
            "A1 the sealed lane is one-shot: FAILED may restart its own"
            " capture/inspection/bars lane from the cache, never re-enter"
            " SEALED_*; a crash after authority consumption is"
            " reconciliation, not a retry"
        ),
    ),
    dict(
        # Replaced 2026-08-23: the original M182 (gutting the explicit
        # UNKNOWN guard in states.is_legal) SURVIVED the full harness because
        # the LEGAL_EDGES whitelist at the next line already refuses every
        # UNKNOWN edge — a redundant guard has no distinct observable
        # behavior. The UNKNOWN-never-a-target invariant stays enforced by
        # the whitelist + test_unknown_is_never_a_target. This replacement
        # targets a decision point that IS load-bearing: the legacy
        # pre-journal era probe.
        id="M182-legacy-era-undetected",
        owner="test_legacy_prejournal_era_detected_exit_3",
        file="scripts/era_status.py",
        anchor=("        if CAPTURE_SCRIPT_TOKEN in cmdline and capture_dir_token in cmdline:"),
        replacement="        if False and capture_dir_token in cmdline:",
        selectors=[f"{U}/test_era_status.py"],
        invariant=(
            "A1 a live pre-journal capture era is UNKNOWN (exit 3), never"
            " presumed idle or failed: silencing the /proc probe lets the"
            " observer invent an outcome the journal never recorded"
        ),
    ),
    dict(
        id="M183-journal-chain-void",
        owner="test_reordering_detected",
        file="src/tree_options/runstate/journal.py",
        anchor=("    if record.prev_record_sha256 != prev_hash:\n        return False"),
        replacement="    if False:\n        return False",
        selectors=[f"{U}/test_runstate_journal.py"],
        invariant=(
            "A1 prev-hash chaining is what detects REORDERED records (each"
            " record still hashes itself correctly); without it history can"
            " be reordered without detection"
        ),
    ),
    dict(
        id="M184-torn-tail-trusted",
        owner="test_truncated_final_line_is_tail_damaged_never_corrupt",
        file="src/tree_options/runstate/journal.py",
        anchor=(
            "            if is_final:\n"
            "                damaged_tail = True\n"
            "                continue  # a torn tail was never acknowledged; exclude it"
        ),
        replacement=(
            "            if is_final:\n"
            "                damaged_tail = False\n"
            "                continue  # a torn tail was never acknowledged; exclude it"
        ),
        selectors=[f"{U}/test_runstate_journal.py"],
        invariant=(
            "A1 a crash-truncated final line must be REPORTED as"
            " tail_damaged; trusting it silently hides that a write was"
            " lost mid-append"
        ),
    ),
    dict(
        id="M185-midfile-corruption-tolerated",
        owner="test_single_record_hash_tamper_detected_midfile",
        file="src/tree_options/runstate/journal.py",
        anchor=(
            "            raise JournalCorruptError(\n"
            "                run_id,\n"
            '                f"journal line {_index + 1} failed decode/hash/chain verification",\n'
            "            )"
        ),
        replacement="            continue",
        selectors=[f"{U}/test_runstate_journal.py"],
        invariant=(
            "A1 damage in a NON-final record means something rewrote"
            " history: an incident, never silently skipped"
        ),
    ),
    dict(
        id="M186-record-hash-not-computed",
        owner="test_chain_verifies_across_many_records",
        file="src/tree_options/runstate/journal.py",
        anchor='    record = record.model_copy(update={"record_sha256": _record_hash(record)})',
        replacement='    record = record.model_copy(update={"record_sha256": ""})',
        selectors=[f"{U}/test_runstate_journal.py"],
        invariant=(
            "A1 the chain is only as strong as the per-record hash"
            " computation; writing an empty hash makes every record fail"
            " verification instead of quietly trusting the stored value"
        ),
    ),
    dict(
        id="M187-live-owner-adopted",
        owner="test_live_owner_not_adopted_even_when_stale_adoption_is_allowed",
        file="src/tree_options/runstate/lease.py",
        anchor=(
            "                if classification is LeaseClassification.HELD or not "
            "allow_stale_adopt:"
        ),
        replacement="                if not allow_stale_adopt:",
        selectors=[f"{U}/test_runstate_lease.py"],
        invariant=(
            "A1 stale-adoption permission never authorizes replacing a"
            " verified live owner's lease; HELD remains a duplicate-launch"
            " refusal even when the stale-only flag is present"
        ),
    ),
    dict(
        id="M188-pid-reuse-taken-as-owner",
        owner="test_pid_reuse_detected_by_starttime",
        file="src/tree_options/runstate/lease.py",
        anchor=(
            "    if live_ticks is None or live_ticks != owner.pid_start_ticks:\n"
            "        return LeaseClassification.STALE_PID_REUSED"
        ),
        replacement=("    if False:\n        return LeaseClassification.STALE_PID_REUSED"),
        selectors=[f"{U}/test_runstate_lease.py"],
        invariant=(
            "A1 a reused pid number is NOT the recorded owner: without the"
            " starttime comparison an unrelated process that inherited the"
            " pid number holds the lease"
        ),
    ),
    dict(
        id="M189-boot-change-taken-as-same-host",
        owner="test_boot_change_classified_stale_even_with_alive_pid",
        file="src/tree_options/runstate/lease.py",
        anchor="    if owner.boot_id != boot_id_now:",
        replacement="    if False:",
        selectors=[f"{U}/test_runstate_lease.py"],
        invariant=(
            "A1 boot identity dominates pid identity: after a reboot a"
            " same-numbered pid is meaningless, alive-looking or not"
            " (the 2026-08-22 reboot already orphaned this era once)"
        ),
    ),
    dict(
        id="M190-stale-threshold-inverted",
        owner="test_fresh_beat_process_state_alive",
        file="src/tree_options/runstate/heartbeat.py",
        anchor="    fresh = (now_epoch - beat.at_epoch) <= stale_after_s",
        replacement="    fresh = (now_epoch - beat.at_epoch) > stale_after_s",
        selectors=[f"{U}/test_runstate_heartbeat.py"],
        invariant=(
            "A1 inverting freshness marks every live process silent and"
            " every dead one fresh, exactly swapping the observability"
            " this module exists to provide"
        ),
    ),
    dict(
        id="M191-unknown-silence-as-terminal",
        owner="test_dead_process_in_resumable_state_is_unknown_resumable",
        file="src/tree_options/runstate/heartbeat.py",
        anchor=(
            "    if state in RESUMABLE_STATES:\n"
            "        return HeartbeatClass.UNKNOWN_RESUMABLE\n"
            "    return HeartbeatClass.DEAD_TERMINAL"
        ),
        replacement=(
            "    if False:\n"
            "        return HeartbeatClass.UNKNOWN_RESUMABLE\n"
            "    return HeartbeatClass.DEAD_TERMINAL"
        ),
        selectors=[f"{U}/test_runstate_heartbeat.py"],
        invariant=(
            "A1 constraint 10: a dead process in a resumable state is"
            " UNKNOWN_RESUMABLE, never terminal — silence is not an"
            " outcome"
        ),
    ),
    dict(
        id="M192-resume-manifest-mismatch-void",
        owner="test_manifest_pin_and_validate_resume",
        file="src/tree_options/runstate/store.py",
        anchor="        if pinned != observed_manifest_sha256:",
        replacement="        if False:",
        selectors=[f"{U}/test_runstate_store.py"],
        invariant=(
            "A1 resume must refuse a manifest the journal never pinned;"
            " voiding the comparison resumes against evidence that may"
            " have been swapped (repair is prohibited, not bypassed)"
        ),
    ),
    dict(
        id="M193-status-repairs-torn-projection",
        owner="test_torn_projection_reported_not_repaired",
        file="scripts/era_status.py",
        anchor=(
            "    except rs_errors.ProjectionTornError as exc:\n"
            '        print(f"PROJECTION TORN: {exc}", file=sys.stderr)\n'
            "        return 2"
        ),
        replacement=(
            "    except rs_errors.ProjectionTornError:\n"
            "        store.rebuild_projection(now_epoch=now_epoch)\n"
            "        return 0"
        ),
        selectors=[f"{U}/test_era_status.py"],
        invariant=(
            "A1 the status command is READ-ONLY: a torn projection is"
            " reported for a lease-holding writer to repair, never quietly"
            " rebuilt by an observer (the mtime assertion pins this)"
        ),
    ),
    dict(
        id="M194-universe-product-void",
        owner="test_verify_universe_refuses_a_rehashed_wrong_expected_masters",
        file="src/tree_options/data/coverage_census.py",
        anchor="    if universe.expected_masters != expected:",
        replacement="    if False:",
        selectors=[f"{U}/test_coverage_census.py"],
        invariant=(
            "A2 expected_masters is the product of the declared grid;"
            " voiding the check lets a tampered-and-re-hashed manifest"
            " under-declare the census population"
        ),
    ),
    dict(
        id="M195-missing-pair-as-complete",
        owner="test_classify_pair_matrix_covers_every_class",
        file="src/tree_options/data/coverage_census.py",
        anchor='        return "MISSING"',
        replacement='        return "COMPLETE"',
        selectors=[f"{U}/test_coverage_census.py"],
        invariant=(
            "A2 a pair with no manifest entry (or no file) is MISSING,"
            " never COMPLETE: coverage totals must not count absence"
        ),
    ),
    dict(
        id="M196-taxonomy-tag-movable",
        owner="test_registry_disagreement_refused",
        file="src/tree_options/data/coverage_census.py",
        anchor=(
            "        if placed != declared_class:\n"
            "            raise CensusTaxonomyError(\n"
            '                f"fact {fact_id!r} registry says {declared_class!r}'
            ' but it sits in {placed!r}"\n'
            "            )"
        ),
        replacement=(
            "        if placed != declared_class:\n            pass  # taxonomy drift accepted"
        ),
        selectors=[f"{U}/test_coverage_census.py"],
        invariant=(
            "A2 a value may never move class implicitly: registry and"
            " section placement must agree or the census refuses to load"
        ),
    ),
    dict(
        id="M197-holiday-session-flip",
        owner="test_holiday_friday_gap_is_not_incomplete",
        file="src/tree_options/data/coverage_census.py",
        anchor=(
            '        return "SPOT_MISSING_HOLIDAY" if not is_session else "SPOT_MISSING_SESSION"'
        ),
        replacement=(
            '        return "SPOT_MISSING_SESSION" if not is_session else "SPOT_MISSING_HOLIDAY"'
        ),
        selectors=[f"{U}/test_coverage_census.py"],
        invariant=(
            "A2 a holiday Friday has no close by definition (not incomplete);"
            " a session Friday without a close is a vendor availability gap"
            " (incomplete) -- flipping the two launders real gaps"
        ),
    ),
    dict(
        id="M198-partial-era-exits-zero",
        owner="test_exit_5_and_census_emitted_when_a_pair_is_missing",
        file="scripts/build_coverage_census.py",
        anchor="    return 5",
        replacement="    return 0",
        selectors=[f"{U}/test_coverage_census.py"],
        invariant=(
            "A2 an incomplete era must exit nonzero even though the census"
            " artifact is still emitted -- partial evidence is never reported"
            " as whole"
        ),
    ),
    dict(
        id="M199-census-manifest-verify-skipped",
        owner="test_manifest_verification_failure_exits_2",
        file="scripts/build_coverage_census.py",
        anchor=(
            "verify_massive_capture_manifest(manifest, capture_dir, capture_version=CAPTURE_VERSION)"
        ),
        replacement="pass  # verify skipped",
        selectors=[f"{U}/test_coverage_census.py"],
        invariant=(
            "A2 the census only consumes a SEALED capture: a manifest that"
            " fails on-disk reconciliation must refuse (exit 2) before any"
            " fact is derived from it"
        ),
    ),
    # ---- A3 protocol 0.2.1 amendment builder (M200-M207) ---------------------
    dict(
        id="M200-stale-census-accepted",
        owner="test_census_content_hash_tamper_refused",
        file="src/tree_options/protocol/amendment.py",
        anchor="        verify_census(census)",
        replacement="        pass",
        selectors=[f"{U}/test_protocol_amendment.py"],
        invariant=(
            "A3 the census is re-hashed at build time; accepting a census"
            " whose declared content hash no longer matches its bytes lets a"
            " tampered census mint an amendment proposal"
        ),
    ),
    dict(
        id="M201-census-manifest-drift-accepted",
        owner="test_census_manifest_drift_refused",
        file="src/tree_options/protocol/amendment.py",
        anchor="    if manifest_sha256 != census.provenance.input_manifest_sha256:",
        replacement="    if False:",
        selectors=[f"{U}/test_protocol_amendment.py"],
        invariant=(
            "A3 the staleness double-check: the census must describe the"
            " capture manifest ON DISK NOW, else the proposal is grounded in"
            " evidence that was swapped after the census ran"
        ),
    ),
    dict(
        id="M202-base-version-unchecked",
        owner="test_wrong_base_version_refused",
        file="src/tree_options/protocol/amendment.py",
        anchor="    if base_version != BASE_PROTOCOL_VERSION:",
        replacement="    if False:",
        selectors=[f"{U}/test_protocol_amendment.py"],
        invariant=(
            "A3 the amendment must build on exactly the ratified 0.2.0 base;"
            " an unchecked base lets a newer/older protocol be amended in"
            " place (the error-message assertion separates this from the"
            " non-monotonic-target refusal)"
        ),
    ),
    dict(
        id="M203-hidden-default-threshold",
        owner="test_missing_flow_min_session_volume_refused",
        file="src/tree_options/protocol/amendment.py",
        anchor="    if flow is None or flow.value <= 0:",
        replacement="    if False:",
        selectors=[f"{U}/test_protocol_amendment.py"],
        invariant=(
            "A3 a missing or zero flow_min_session_volume is exactly the"
            " silent default the builder exists to prevent: the owner must"
            " supply a real positive threshold or nothing is proposed"
        ),
    ),
    dict(
        id="M204-bool-as-int",
        owner="test_owner_value_bool_true_rejected",
        file="src/tree_options/protocol/amendment.py",
        anchor="        if isinstance(v, bool):",
        replacement="        if False:",
        selectors=[f"{U}/test_protocol_amendment.py"],
        invariant=(
            "A3 pydantic lax mode coerces YAML/JSON true to 1; only the"
            " explicit bool guard keeps a boolean from becoming a threshold"
            " value"
        ),
    ),
    dict(
        id="M205-value-rule-mismatch-accepted",
        owner="test_derived_value_not_equal_to_rule_refused",
        file="src/tree_options/protocol/amendment.py",
        anchor="            if computed != ov.value:",
        replacement="            if False:",
        selectors=[f"{U}/test_protocol_amendment.py"],
        invariant=(
            "A3 an owner value with derivation provenance must equal what its"
            " ratified rule computes; accepting a mismatch launders a"
            " hand-picked number as census-derived"
        ),
    ),
    dict(
        id="M206-future-derived-fact",
        owner="test_future_derived_fact_refused",
        file="src/tree_options/protocol/amendment.py",
        anchor=('                if census.value_registry.get(fid) != "observed_census_fact":'),
        replacement="                if False:",
        selectors=[f"{U}/test_protocol_amendment.py"],
        invariant=(
            "A3 only facts the census classes observed_census_fact exist"
            " yet; deriving from a predeclared/not-yet-decided id smuggles a"
            " future value (the G3 bar-volume contradiction) into the"
            " proposal"
        ),
    ),
    dict(
        id="M207-tracked-output-write",
        owner="test_out_root_outside_artifacts_refused",
        file="src/tree_options/protocol/amendment.py",
        anchor="    if not resolved_out_root.is_relative_to(artifacts_root):",
        replacement="    if False:",
        selectors=[f"{U}/test_protocol_amendment.py"],
        invariant=(
            "A3 the builder is dry-run only: confining writes to artifacts/"
            " is what makes it structurally incapable of touching a tracked"
            " file such as research_protocol.yaml"
        ),
    ),
    # ---- A5 G4 seal authority (identity/ledger/preflight/execute, M213-M218) --
    dict(
        id="M213-content-identity-includes-code",
        owner="test_content_identity_stable_across_code_sha_change_while_run_id_changes",
        file="src/tree_options/seal/identity.py",
        anchor=(
            'blanked = identity.model_copy(update={"code_sha": "", "verified_packet_sha256": ""})'
        ),
        replacement=(
            'blanked = identity.model_copy(update={"code_sha": identity.code_sha, '
            '"verified_packet_sha256": ""})'
        ),
        selectors=[f"{U}/test_seal_identity.py"],
        invariant=(
            "A5 the content identity BLANKS code_sha: two checkouts of the"
            " same research content share it, so a second consumption under"
            " either id is refused; hashing the full identity makes every"
            " fresh checkout fresh authority"
        ),
    ),
    # M214 was the round-1 duplicate-guard anchor on stored ids; F7
    # removed that comparison (replaced by recompute-from-payload), so
    # the mutant's anchor no longer exists in scripts/g4_seal.py. The
    # fix is load-bearing; the new round-1 equivalent is M229 below
    # (stored-ids agreement check voided). The M214 slot is intentionally
    # NOT in the MUTANTS list — an anchored entry here would be classified
    # MUTATION_DRIFT and fail the harness's exit-0 gate. The invariant
    # survives in M229.
    dict(
        id="M229-stored-consumption-ids-trusted",
        owner="test_forged_consumption_stored_ids_refused_as_corrupt",
        file="scripts/g4_seal.py",
        anchor=(
            "        if record.sealed_run_id != record_run_id"
            " or record.content_identity != record_content_id:"
        ),
        replacement="        if False:",
        selectors=[f"{U}/test_g4_seal.py"],
        invariant=(
            "F7 the duplicate guard RECOMPUTES sealed_run_id/content_identity"
            " from each CONSUMPTION record's own identity payload and refuses"
            " when the stored ids disagree (corruption, never a skip); trusting"
            " stored ids reopens the forged-replay bypass where a chain-valid"
            " record carries the target payload under adversarial stored ids"
        ),
    ),
    dict(
        id="M215-approval-reverify-void",
        owner="test_approval_tampered_payload_exit_6",
        file="scripts/g4_seal.py",
        anchor=("        and sealed_run_id(record.identity) == run_id"),
        replacement="        and True",
        selectors=[f"{U}/test_g4_seal.py"],
        invariant=(
            "A5 the approval is RECOMPUTED from the record's own payload and"
            " compared to this run's sealed_run_id; trusting the stored id"
            " lets a forged (chain-valid, mismatched-payload) approval spend"
            " authority it never covered"
        ),
    ),
    dict(
        id="M216-verdict-leak",
        owner="test_preflight_all_verified_verdict_is_null_and_not_computed",
        file="scripts/g4_seal.py",
        anchor=(
            "    report = PreflightReport(\n"
            "        verdict=None,\n"
            "        verdict_computed=False,\n"
            "        criteria_inputs=statuses,\n"
            "        verified_inputs=packet,\n"
            "    )"
        ),
        replacement=(
            "    report = PreflightReport(\n"
            "        verdict=None,\n"
            "        verdict_computed=any(s.available for s in statuses.values()),\n"
            "        criteria_inputs=statuses,\n"
            "        verified_inputs=packet,\n"
            "    )"
        ),
        selectors=[f"{U}/test_g4_seal.py"],
        invariant=(
            "A5 preflight is structurally verdict-free: the output model pins"
            " verdict to Literal[None] and verdict_computed to Literal[False],"
            " so any coerced verdict is a validation error and the owner test"
            " asserts the literal JSON never carries one"
        ),
    ),
    dict(
        id="M217-tmp-ledger-accepted",
        owner="test_tmp_root_refused",
        file="src/tree_options/seal/ledger.py",
        anchor="if resolved == TMP_AUTHORITY_ROOT or TMP_AUTHORITY_ROOT in resolved.parents:",
        replacement="if False:",
        selectors=[f"{U}/test_seal_ledger.py"],
        invariant=(
            "A5 authority never lives under /tmp (wiped on reboot): the host"
            " rule is a mechanical resolved-path prefix check in"
            " validate_ledger_root"
        ),
    ),
    dict(
        id="M218-consume-after-work",
        owner="test_first_execution_consumption_durable_before_runner_gets_same_held_bytes",
        file="scripts/g4_seal.py",
        anchor=(
            "    consumption_sha = seal_ledger.append_record(ledger_root, consumption_record)\n"
            "    outcome = runner(current)"
        ),
        replacement=(
            "    outcome = runner(current)\n"
            "    consumption_sha = seal_ledger.append_record(ledger_root, consumption_record)"
        ),
        selectors=[f"{U}/test_g4_seal.py"],
        invariant=(
            "A5 the CONSUMPTION record is durable (flock + fsync) BEFORE the"
            " runner is invoked; consuming after the work re-opens the"
            " crash-window where the sealed run happened but no authority"
            " was spent"
        ),
    ),
    # ---- PR A4 (bars era): appended after M218 in FILE ORDER, not numeric order --
    dict(
        id="M208-protocol-gate-void",
        owner="test_preflight_exit_2_wrong_version_even_with_matching_record",
        file="scripts/launch_bars_era.py",
        anchor=(
            "if protocol.meta.protocol_version != REQUIRED_BARS_PROTOCOL_VERSION"
            " or approval is None:"
        ),
        replacement="if False:",
        selectors=[f"{U}/test_launch_bars_era.py"],
        invariant=(
            "A4 preflight gate 1: the loaded protocol must be EXACTLY 0.2.1 and"
            " a BARS_LAUNCH_APPROVAL record must bind its hash; a record bound"
            " to the current 0.2.0 hash does not open the gate (exit 2 is the"
            " documented correct answer on main)"
        ),
    ),
    dict(
        id="M209-authority-record-void",
        owner="test_execute_exit_6_when_record_binds_other_work_manifest",
        file="scripts/launch_bars_era.py",
        anchor="approvals = [r for r in approvals if r.work_manifest_sha256 == work_manifest_sha]",
        replacement="approvals = list(approvals)",
        selectors=[f"{U}/test_launch_bars_era.py"],
        invariant=(
            "A4 execute authority gate: a BARS_LAUNCH_APPROVAL record must bind"
            " THIS work manifest's sha256 — approval granted for one manifest"
            " never transfers to another (exit 6, nothing consumed)"
        ),
    ),
    dict(
        id="M210-manifest-order-nondeterministic",
        owner="test_order_entries_canonical_from_shuffled",
        file="src/tree_options/data/bars_manifest.py",
        anchor="return tuple(sorted(entries, key=_entry_order_key))",
        replacement="return tuple(entries)",
        selectors=[f"{U}/test_bars_manifest.py"],
        invariant=(
            "A4 work-manifest entries are ordered deterministically (underlying,"
            " as_of, expiry, strike-rank, call-before-put, ticker); the model"
            " validator refuses any other order, so regeneration is byte-identical"
        ),
    ),
    dict(
        id="M211-override-fallback-accepted",
        owner="test_every_override_flag_refused_exit_4",
        file="scripts/launch_bars_era.py",
        anchor="if provided is not None and provided != pinned:",
        replacement="if False:",
        selectors=[f"{U}/test_launch_bars_era.py"],
        invariant=(
            "A4 refuse-fallback: --vendor-host/--endpoint-template/--calendar-token"
            "/--universe/--selection-rule overrides are refused outright (exit 4);"
            " the pinned constants are the only accepted values and no code path"
            " substitutes a fallback"
        ),
    ),
    dict(
        id="M212-duplicate-launch-accepted",
        owner="test_preflight_exit_5_on_held_lease_duplicate_launch",
        file="scripts/launch_bars_era.py",
        anchor="if lease_module.owner_exists(store_dir):",
        replacement="if False:",
        selectors=[f"{U}/test_launch_bars_era.py"],
        invariant=(
            "A4 duplicate launch: a HELD lease for the same run refuses preflight"
            " (exit 5) — a live owner is presumed working; 'no log output' is"
            " not evidence of death"
        ),
    ),
    # ---- external PR #13 audit: canonical durable run identity -----------------
    dict(
        id="M230-noncanonical-run-id-accepted",
        owner="test_create_refuses_noncanonical_run_id_before_filesystem_mutation",
        file="src/tree_options/runstate/store.py",
        anchor=(
            "        _validate_canonical_run_id(root, identity)\n"
            "        root_fd = custody.open_directory("
        ),
        replacement="        root_fd = custody.open_directory(",
        selectors=[f"{U}/test_runstate_store.py"],
        invariant=(
            "PR13 one logical RunIdentityCore has exactly one computed store id;"
            " an operator-supplied alternate id refuses before any directory is created"
        ),
    ),
    dict(
        id="M231-universe-source-id-host-contaminated",
        owner="test_two_physical_checkout_roots_render_byte_identical_universe",
        file="scripts/gen_coverage_universe.py",
        anchor="    return validate_source_id(relative.as_posix())",
        replacement="    return physical.as_posix().lstrip('/')",
        selectors=[f"{U}/test_gen_coverage_universe.py"],
        invariant=(
            "PR13 the universe records the wrapper's repo-relative logical id;"
            " two physical clone roots render byte-identical artifacts"
        ),
    ),
    dict(
        id="M232-universe-wrapper-bytes-unbound",
        owner="test_wrapper_byte_change_changes_universe_identity",
        file="scripts/gen_coverage_universe.py",
        anchor='        source_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),',
        replacement='        source_sha256="0" * 64,',
        selectors=[f"{U}/test_gen_coverage_universe.py"],
        invariant=(
            "PR13 checkout location is excluded, but the exact wrapper bytes remain"
            " bound through source_sha256 and the universe content hash"
        ),
    ),
    dict(
        id="M233-absolute-universe-source-id-accepted",
        owner="test_rehashed_absolute_source_id_is_refused",
        file="src/tree_options/data/coverage_census.py",
        anchor="        logical_source.is_absolute()",
        replacement="        False",
        selectors=[f"{U}/test_gen_coverage_universe.py"],
        invariant=(
            "PR13 even a correctly rehashed universe cannot carry a host-absolute source identity"
        ),
    ),
    # ---- external PR #13 audit: complete run-state filesystem custody ---------
    dict(
        id="M234-runstate-component-nofollow-removed",
        owner="test_create_refuses_intermediate_ancestor_symlink_without_writing_target",
        file="src/tree_options/runstate/custody.py",
        anchor="_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW",
        replacement="_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY",
        selectors=[f"{U}/test_runstate_custody.py"],
        invariant=(
            "PR13 every lexical ancestor is opened O_DIRECTORY|O_NOFOLLOW;"
            " a durable-looking root may never traverse an intermediate symlink"
        ),
    ),
    dict(
        id="M235-runstate-hard-links-accepted",
        owner="test_open_refuses_run_json_hard_link",
        file="src/tree_options/runstate/custody.py",
        anchor=(
            "    if st.st_nlink != 1:\n"
            '        _refuse(run_id, f"{purpose} has unexpected link count '
            '{st.st_nlink}, expected 1")'
        ),
        replacement="    if False:\n        pass",
        selectors=[f"{U}/test_runstate_custody.py"],
        invariant=(
            "PR13 every run-state authority file has exactly one link; a"
            " planted hard link must not turn another inode name into authority"
        ),
    ),
    dict(
        id="M236-runstate-atomic-final-symlink-accepted",
        owner="test_projection_final_symlink_refuses_without_mutating_repo_target",
        file="src/tree_options/runstate/custody.py",
        anchor=(
            "def _safe_existing_name(\n"
            "    parent_fd: int,\n"
            "    name: str,\n"
            "    *,\n"
            "    run_id: str,\n"
            "    purpose: str,\n"
            ") -> os.stat_result | None:\n"
            "    try:\n"
            "        existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)"
        ),
        replacement=(
            "def _safe_existing_name(\n"
            "    parent_fd: int,\n"
            "    name: str,\n"
            "    *,\n"
            "    run_id: str,\n"
            "    purpose: str,\n"
            ") -> os.stat_result | None:\n"
            "    try:\n"
            "        existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=True)"
        ),
        selectors=[f"{U}/test_runstate_custody.py"],
        invariant=(
            "PR13 atomic writers classify the final name itself without"
            " following it; replacing a symlink is a refusal even when its target is regular"
        ),
    ),
    dict(
        id="M237-runstate-exclusive-temp-custody-removed",
        owner="test_projection_temp_symlink_refuses_without_mutating_target",
        file="src/tree_options/runstate/custody.py",
        anchor="                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,",
        replacement="                os.O_RDWR | os.O_CREAT | os.O_TRUNC,",
        selectors=[f"{U}/test_runstate_custody.py"],
        invariant=(
            "PR13 temporary publish objects are O_EXCL|O_NOFOLLOW; a planted"
            " temp name must refuse before target bytes can be truncated"
        ),
    ),
    dict(
        id="M238-runstate-published-bytes-unverified",
        owner="test_projection_in_place_rewrite_after_publish_is_refused",
        file="src/tree_options/runstate/custody.py",
        anchor="            if read_all(published_fd) != payload:",
        replacement="            if False:",
        selectors=[f"{U}/test_runstate_custody.py"],
        invariant=(
            "PR13 an atomic publish returns only after re-reading the exact"
            " published bytes; an in-place rewrite of the same inode is reconciliation"
        ),
    ),
    dict(
        id="M239-runstate-published-inode-unverified",
        owner="test_projection_deletion_recreation_after_publish_is_refused",
        file="src/tree_options/runstate/custody.py",
        anchor=("            if (published_stat.st_dev, published_stat.st_ino) != temp_identity:"),
        replacement="            if False:",
        selectors=[f"{U}/test_runstate_custody.py"],
        invariant=(
            "PR13 byte-identical deletion/recreation is still an inode swap;"
            " the final name must map to the verified exclusive temp inode"
        ),
    ),
    dict(
        id="M240-runstate-parent-substitution-unverified",
        owner="test_projection_parent_rename_and_substitution_is_refused",
        file="src/tree_options/runstate/custody.py",
        anchor=(
            "        finally:\n"
            "            os.close(published_fd)\n"
            "        verify_directory_identity(directory_path, directory_fd, run_id=run_id)"
        ),
        replacement="        finally:\n            os.close(published_fd)",
        selectors=[f"{U}/test_runstate_custody.py"],
        invariant=(
            "PR13 success requires the lexical store path still to name the"
            " held directory; parent rename/substitution is reconciliation"
        ),
    ),
    dict(
        id="M241-runstate-lock-inode-unbound",
        owner="test_lease_lock_deletion_recreation_refuses_before_owner_publish",
        file="src/tree_options/runstate/lease.py",
        anchor=(
            "        try:\n"
            "            custody.verify_name_identity(\n"
            "                lease_fd,\n"
            '                "adopt.lock",\n'
            "                lock_fd,\n"
            "                run_id=run_id,\n"
            '                purpose="lease adopt.lock",\n'
            "            )\n"
            "            raw = custody.read_named_bytes("
        ),
        replacement="        try:\n            raw = custody.read_named_bytes(",
        selectors=[f"{U}/test_runstate_custody.py"],
        invariant=(
            "PR13 lease mutation verifies adopt.lock still names the flocked"
            " inode before publishing owner authority"
        ),
    ),
    dict(
        id="M242-runstate-journal-name-inode-unbound",
        owner="test_journal_name_clone_swap_during_append_is_refused",
        file="src/tree_options/runstate/journal.py",
        anchor=(
            "                os.fsync(fd)\n"
            "                custody.verify_name_identity(\n"
            "                    dir_fd,\n"
            "                    JOURNAL_FILENAME,\n"
            "                    fd,\n"
            "                    run_id=run_id,\n"
            '                    purpose="journal.jsonl authority",\n'
            "                )\n"
            "                post = _locked_tail_view(fd)"
        ),
        replacement="                os.fsync(fd)\n                post = _locked_tail_view(fd)",
        selectors=[f"{U}/test_runstate_custody.py"],
        invariant=(
            "PR13 a journal append returns only while journal.jsonl still"
            " names the flocked inode; a clone swap cannot create two authority tails"
        ),
    ),
    dict(
        id="M243-runstate-immutable-identity-rewrite-accepted",
        owner="test_in_place_run_identity_rewrite_is_refused_on_rebind",
        file="src/tree_options/runstate/store.py",
        anchor="            if observed != self.identity:",
        replacement="            if False:",
        selectors=[f"{U}/test_runstate_custody.py"],
        invariant=(
            "PR13 run.json is immutable in full, including process-incarnation"
            " fields excluded from deterministic run-id computation"
        ),
    ),
    # ---- PR13 G4 typed verified-input packet and effect-boundary join -------
    dict(
        id="M244-g4-cboe-foreign-schema-accepted",
        owner="test_correctly_self_hashed_foreign_manifest_version_refuses",
        file="src/tree_options/data/cboe_eod.py",
        anchor="if manifest.schema_version != REAL_OPTIONS_SCHEMA_VERSION:",
        replacement="if False:",
        selectors=[f"{U}/test_g4_verified_inputs.py"],
        invariant="G4 lane 1 accepts only the pinned Cboe manifest schema version",
    ),
    dict(
        id="M245-g4-cboe-real-verifier-bypassed",
        owner="test_missing_or_tampered_referenced_payload_refuses",
        file="src/tree_options/seal/verified_inputs.py",
        anchor=(
            "        verify_real_options_manifest(\n"
            "            manifest,\n"
            "            result,\n"
            "            overlay=overlay,\n"
            "            source_bytes=source_raw,\n"
            "        )"
        ),
        replacement="        pass  # MUTATED: Cboe semantic verifier bypassed",
        selectors=[f"{U}/test_g4_verified_inputs.py"],
        invariant="G4 lane 1 calls the real Cboe manifest and payload verifier",
    ),
    dict(
        id="M246-g4-massive-foreign-schema-accepted",
        owner="test_correctly_self_hashed_foreign_manifest_version_refuses",
        file="src/tree_options/data/massive_manifest.py",
        anchor="if manifest.schema_version != MASSIVE_MANIFEST_SCHEMA_VERSION:",
        replacement="if False:",
        selectors=[f"{U}/test_g4_verified_inputs.py"],
        invariant="G4 lane 2 accepts only the pinned Massive manifest schema",
    ),
    dict(
        id="M247-g4-massive-foreign-capture-accepted",
        owner="test_correctly_self_hashed_foreign_massive_capture_version_refuses",
        file="src/tree_options/data/massive_manifest.py",
        anchor="if manifest.capture_version != capture_version:",
        replacement="if False:",
        selectors=[f"{U}/test_g4_verified_inputs.py"],
        invariant="G4 lane 2 accepts only the predeclared m4b-capture/1 producer",
    ),
    dict(
        id="M248-g4-massive-real-verifier-bypassed",
        owner="test_massive_unlisted_json_is_reconciled_from_held_directory",
        file="src/tree_options/seal/verified_inputs.py",
        anchor=(
            "            verify_massive_capture_manifest(\n"
            "                manifest,\n"
            "                paths.lane2_manifest.parent,\n"
            "                capture_version=EXPECTED_MASSIVE_CAPTURE_VERSION,\n"
            "                captured_files=held_by_path,\n"
            "                observed_json_files=observed,\n"
            "            )"
        ),
        replacement="            pass  # MUTATED: Massive reconciliation bypassed",
        selectors=[f"{U}/test_g4_verified_inputs.py"],
        invariant="G4 lane 2 calls the real Massive bytes and disk-census verifier",
    ),
    dict(
        id="M249-g4-criteria-identifiers-unfrozen",
        owner="test_criteria_identifiers_are_exact_and_ordered",
        file="src/tree_options/seal/verified_inputs.py",
        anchor="if ids != CRITERION_IDS:",
        replacement="if False:",
        selectors=[f"{U}/test_g4_verified_inputs.py"],
        invariant="The typed criteria artifact carries the exact six frozen criterion ids",
    ),
    dict(
        id="M250-g4-criteria-source-join-void",
        owner="test_stale_criteria_source_document_sha_refuses",
        file="src/tree_options/seal/verified_inputs.py",
        anchor="if criteria.source_sha256 != criteria_source_sha:",
        replacement="if False:",
        selectors=[f"{U}/test_g4_verified_inputs.py"],
        invariant="The held criteria artifact is joined to the held frozen source document",
    ),
    dict(
        id="M251-g4-calendar-enum-opened",
        owner="test_correctly_self_hashed_foreign_calendar_decision_refuses",
        file="src/tree_options/seal/verified_inputs.py",
        anchor=('CalendarDecision = Literal["repo-generated-calendar", "weekend-only-accepted"]'),
        replacement="CalendarDecision = str",
        selectors=[f"{U}/test_g4_verified_inputs.py"],
        invariant="The owner calendar decision is one of the two predeclared choices",
    ),
    dict(
        id="M252-g4-calendar-content-unbound",
        owner="test_calendar_decision_content_hash_rejects_typed_body_tamper",
        file="src/tree_options/seal/verified_inputs.py",
        anchor=(
            "        if self.content_sha256 != expected:\n"
            '            raise ValueError("calendar decision content_sha256 does not bind the typed body")'
        ),
        replacement='        if False:\n            raise ValueError("MUTATED")',
        selectors=[f"{U}/test_g4_verified_inputs.py"],
        invariant="The typed owner calendar decision self-hash binds its complete body",
    ),
    dict(
        id="M253-g4-packet-content-unbound",
        owner="test_packet_self_hash_rejects_caller_tamper",
        file="src/tree_options/seal/verified_inputs.py",
        anchor=(
            "        if self.packet_content_sha256 != expected:\n"
            '            raise ValueError("packet_content_sha256 does not bind the verified-input body")'
        ),
        replacement='        if False:\n            raise ValueError("MUTATED")',
        selectors=[f"{U}/test_g4_verified_inputs.py"],
        invariant="VerifiedSealedInputs is immutable and self-binding over every field",
    ),
    dict(
        id="M254-g4-input-directory-nofollow-removed",
        owner="test_intermediate_manifest_directory_symlink_is_never_followed",
        file="src/tree_options/seal/input_custody.py",
        anchor="_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW",
        replacement="_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY",
        selectors=[f"{U}/test_g4_verified_inputs.py"],
        invariant="Every G4 input path component is opened O_DIRECTORY|O_NOFOLLOW",
    ),
    dict(
        id="M255-g4-input-hard-links-accepted",
        owner="test_hard_linked_manifest_is_refused",
        file="src/tree_options/seal/input_custody.py",
        anchor="if st.st_nlink != 1:",
        replacement="if False:",
        selectors=[f"{U}/test_g4_verified_inputs.py"],
        invariant="Every held G4 input file has one unambiguous directory name",
    ),
    dict(
        id="M256-g4-input-inplace-rewrite-unnoticed",
        owner="test_in_place_rewrite_during_single_read_is_refused",
        file="src/tree_options/seal/input_custody.py",
        anchor=(
            "if _stable_file_fields(before) != _stable_file_fields(after) "
            "or len(raw) != after.st_size:"
        ),
        replacement="if False:",
        selectors=[f"{U}/test_g4_verified_inputs.py"],
        invariant="A G4 input cannot change while its single held-inode read is in progress",
    ),
    dict(
        id="M257-g4-protocol-raw-hash-substituted",
        owner="test_protocol_hash_comes_from_the_validated_protocol_model",
        file="src/tree_options/seal/verified_inputs.py",
        anchor="protocol_sha = protocol_hash(load_protocol_bytes(protocol_raw))",
        replacement="protocol_sha = sha256_hex(protocol_raw)",
        selectors=[f"{U}/test_g4_verified_inputs.py"],
        invariant="G4 binds the canonical hash of the validated protocol model",
    ),
    dict(
        id="M258-g4-checkout-movement-unnoticed",
        owner="test_checkout_movement_during_verification_refuses",
        file="src/tree_options/seal/verified_inputs.py",
        anchor="if after_sha != before_sha:",
        replacement="if False:",
        selectors=[f"{U}/test_g4_verified_inputs.py"],
        invariant="The checkout SHA is stable across all held input reads",
    ),
    dict(
        id="M259-g4-current-packet-crossjoin-void",
        owner="test_current_packet_must_equal_approved_packet",
        file="scripts/g4_seal.py",
        anchor="if current.packet != expected_packet:",
        replacement="if False:",
        selectors=[f"{U}/test_g4_seal.py"],
        invariant="Execution re-verifies current bytes and equals them to the approved packet",
    ),
    dict(
        id="M260-g4-runner-version-crossjoin-void",
        owner="test_runner_version_is_cross_joined_before_consumption",
        file="scripts/g4_seal.py",
        anchor="if presented_runner_version != expected_packet.runner_version:",
        replacement="if False:",
        selectors=[f"{U}/test_g4_seal.py"],
        invariant="The invoked runner machinery version equals the approved packet",
    ),
    dict(
        id="M261-g4-ledger-packet-hash-not-bound",
        owner="test_first_execution_consumption_durable_before_runner_gets_same_held_bytes",
        file="src/tree_options/seal/verified_inputs.py",
        anchor="verified_packet_sha256=packet.packet_content_sha256,",
        replacement='verified_packet_sha256="0" * 64,',
        selectors=[f"{U}/test_g4_seal.py"],
        invariant="APPROVAL and CONSUMPTION identities explicitly carry the verified packet hash",
    ),
    dict(
        id="M262-g4-runner-rereads-paths",
        owner="test_runner_uses_preconsumption_held_bundle_when_paths_move_during_append",
        file="scripts/g4_seal.py",
        anchor="outcome = runner(current)",
        replacement="outcome = runner(verify_sealed_inputs(inputs, git_runner=git_runner))",
        selectors=[f"{U}/test_g4_seal.py"],
        invariant="The runner receives the same pre-consumption held bytes, never a path re-read",
    ),
    dict(
        id="M263-g4-lane1-payload-set-unbound",
        owner="test_verified_packet_comes_only_from_real_typed_verifiers",
        file="src/tree_options/seal/verified_inputs.py",
        anchor=(
            "        manifest_version=manifest.schema_version,\n"
            "        referenced_payload_set_hash=_payload_set_hash(payloads),"
        ),
        replacement=(
            "        manifest_version=manifest.schema_version,\n"
            "        referenced_payload_set_hash=sha256_hex(manifest_raw),"
        ),
        selectors=[f"{U}/test_g4_verified_inputs.py"],
        invariant="Lane 1 packet binding separately hashes its referenced payload set",
    ),
    dict(
        id="M264-g4-lane2-payload-set-unbound",
        owner="test_verified_packet_comes_only_from_real_typed_verifiers",
        file="src/tree_options/seal/verified_inputs.py",
        anchor=(
            "        manifest_version=MASSIVE_MANIFEST_SCHEMA_VERSION,\n"
            "        referenced_payload_set_hash=_payload_set_hash(payloads),"
        ),
        replacement=(
            "        manifest_version=MASSIVE_MANIFEST_SCHEMA_VERSION,\n"
            "        referenced_payload_set_hash=sha256_hex(manifest_raw),"
        ),
        selectors=[f"{U}/test_g4_verified_inputs.py"],
        invariant="Lane 2 packet binding separately hashes every master/bar/spot payload",
    ),
    dict(
        id="M265-g4-dirty-checkout-accepted",
        owner="test_preflight_dirty_tracked_tree_unavailable",
        file="src/tree_options/seal/verified_inputs.py",
        anchor="if dirty:",
        replacement="if False:",
        selectors=[f"{U}/test_g4_seal.py"],
        invariant="A tracked-dirty checkout cannot produce a verified G4 packet",
    ),
    dict(
        id="M266-g4-effect-boundary-ledger-recheck-void",
        owner=(
            "test_interleaved_consumption_after_input_verification_is_refused_at_effect_boundary"
        ),
        file="scripts/g4_seal.py",
        anchor=(
            "    # tail while holding the ledger lock.\n"
            "    view = read_ledger(ledger_root)\n"
            "    _check_authority(view, identity)"
        ),
        replacement=(
            "    # tail while holding the ledger lock.\n    view = read_ledger(ledger_root)"
        ),
        selectors=[f"{U}/test_g4_seal.py"],
        invariant="Approval and duplicate authority are rechecked at the final spend boundary",
    ),
    dict(
        id="M267-g4-execute-packet-selfcheck-void",
        owner="test_execute_revalidates_packet_self_hash_before_ledger_access",
        file="scripts/g4_seal.py",
        anchor=(
            "    try:\n"
            "        expected_packet = VerifiedSealedInputs.model_validate_json(\n"
            "            expected_packet.model_dump_json()\n"
            "        )\n"
            "    except Exception as exc:\n"
            "        raise VerifiedInputsError(\n"
            '            "packet", f"expected packet self-validation failed: {exc}"\n'
            "        ) from None"
        ),
        replacement="    expected_packet = expected_packet  # MUTATED: self-check bypassed",
        selectors=[f"{U}/test_g4_seal.py"],
        invariant="Execute revalidates packet self-binding even after low-level model construction",
    ),
    # ---- real-lane wave 3 (w5 holdout + w3 liquidity + w2 earnings + w7a
    # drop + G5 null + G1 adapter, M268-M289) --------------------------------
    dict(
        id="M268-w5-refusal-dropped",
        owner="test_sealed_test_sessions_are_refused_before_registration",
        file="src/tree_options/trials/options_run.py",
        anchor="    if sealed_test_intersections:",
        replacement="    if False:",
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "w5 (verdict D7.1) a fold TEST session inside the sealed window"
            " refuses BEFORE registration: dropping the refusal lets a"
            " mis-declared grid spend the seal, register the leaking trial,"
            " and burn its config budget"
        ),
    ),
    dict(
        id="M269-w5-intersection-inverted",
        owner="test_execution_tail_seal_consumption_is_tagged_not_refused",
        file="src/tree_options/trials/options_run.py",
        anchor=(
            "    sealed_test_intersections = sorted(\n"
            "        {\n"
            "            session.isoformat()\n"
            "            for fold in folds\n"
            "            for session in fold.test_sessions\n"
            "            if session.isoformat() in _SEALED_HOLDOUT_SESSIONS\n"
            "        }\n"
            "    )"
        ),
        replacement=(
            "    sealed_test_intersections = sorted(\n"
            "        {\n"
            "            session.isoformat()\n"
            "            for fold in folds\n"
            "            for session in fold.test_sessions\n"
            "            if session.isoformat() not in _SEALED_HOLDOUT_SESSIONS\n"
            "        }\n"
            "    )"
        ),
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "w5 inverting the intersection refuses EVERY trial (the offender"
            " set becomes the UNSEALED sessions): a seal the grid never"
            " touches must never block a legitimate run. (P4 re-pin: the"
            " P4 seal-disclosure block added a second identical"
            " comprehension — the anchor now pins the guard-side one via"
            " its assignment line)"
        ),
    ),
    dict(
        id="M270-w5-full-window-overlap-only",
        owner="test_sealed_test_sessions_are_refused_before_registration",
        file="src/tree_options/trials/options_run.py",
        anchor=(
            "    sealed_test_intersections = sorted(\n"
            "        {\n"
            "            session.isoformat()\n"
            "            for fold in folds\n"
            "            for session in fold.test_sessions\n"
            "            if session.isoformat() in _SEALED_HOLDOUT_SESSIONS\n"
            "        }\n"
            "    )"
        ),
        replacement=(
            "    sealed_test_intersections = sorted(\n"
            "        {\n"
            "            session.isoformat()\n"
            "            for fold in folds\n"
            "            if all(s.isoformat() in _SEALED_HOLDOUT_SESSIONS for s in fold.test_sessions)\n"
            "            for session in fold.test_sessions\n"
            "        }\n"
            "    )"
        ),
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "w5 the refusal fires on ANY sealed test session, never only when"
            " a fold's ENTIRE test window is sealed — a partial overlap (the"
            " realistic mis-declaration: 13 sealed Fridays inside longer"
            " windows) must refuse exactly the same"
        ),
    ),
    dict(
        id="M271-w5-tail-disclosure-voided",
        owner="test_execution_tail_seal_consumption_is_tagged_not_refused",
        file="src/tree_options/trials/options_run.py",
        anchor='                "holdout_seal_consumed_sessions": sealed_tail_sessions,',
        replacement='                "holdout_seal_consumed_sessions": [],',
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "w5 (verdict D7.2) the execution-tail consumption of the sealed"
            " window is DISCLOSED per fold: an artifact that stops naming the"
            " consumed sessions silently contains window-A executions"
        ),
    ),
    dict(
        id="M272-w3-dollar-volume-mean",
        owner="test_dollar_volume_median_is_exact_over_distinct_values",
        file="src/tree_options/data/vwap_pit_surface.py",
        anchor=(
            "        median = statistics.median("
            "[rows[session][0] * rows[session][1] for session in window])"
        ),
        replacement=(
            "        median = sum(rows[session][0] * rows[session][1] for session in window)"
            " / len(window)"
        ),
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "w3 the dollar-volume stamp is the exact 20-session MEDIAN of"
            " close*volume: a mean lets one spike day launder the liquidity"
            " term (the 50M fixture repeats one value, where mean == median —"
            " the owner seams the statistic with distinct values and a spike)"
        ),
    ),
    dict(
        id="M273-w3-dollar-volume-window-unbounded",
        owner="test_dollar_volume_median_is_exact_over_distinct_values",
        file="src/tree_options/data/vwap_pit_surface.py",
        anchor="        start_ordinal = end_ordinal - DOLLAR_VOLUME_WINDOW_SESSIONS + 1",
        replacement="        start_ordinal = 0",
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "w3 the median is over EXACTLY the trailing 20 calendar sessions:"
            " anchoring at the calendar start turns it into an all-history"
            " median that drifts with capture length and dilutes staleness"
        ),
    ),
    dict(
        id="M274-w3-dollar-volume-holes-tolerated",
        owner="test_dollar_volume_requires_a_contiguous_20_session_window",
        file="src/tree_options/data/vwap_pit_surface.py",
        anchor="        window = calendar.sessions()[start_ordinal : end_ordinal + 1]",
        replacement=(
            "        window = tuple(\n"
            "            s for s in calendar.sessions()[start_ordinal : end_ordinal + 1] if s in rows\n"
            "        )"
        ),
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "w3 fail-closed on availability: a hole inside the trailing 20 (or"
            " a history shorter than 20) must fall back to the declared"
            " sentinel — median-ing over whatever happened to be captured"
            " launders a partial window as a 20d median"
        ),
    ),
    dict(
        id="M275-w3-short-history-floor-gutted",
        owner="test_dollar_volume_needs_twenty_sessions_of_calendar_history",
        file="src/tree_options/data/vwap_pit_surface.py",
        anchor="        if start_ordinal < 0:",
        replacement="        if False:",
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "w3 a calendar with fewer than 20 sessions of history cannot"
            " answer a 20d median: gutting the floor lets a negative slice"
            " index quietly window from the calendar's start"
        ),
    ),
    dict(
        id="M276-w3-non-session-fallback",
        owner="test_dollar_volume_refuses_a_non_calendar_visible_session",
        file="src/tree_options/data/vwap_pit_surface.py",
        anchor=(
            "        try:\n"
            "            end_ordinal = calendar.ordinal(visible_session)\n"
            "        except NotASessionError:\n"
            "            return None"
        ),
        replacement=(
            "        try:\n"
            "            end_ordinal = calendar.ordinal(visible_session)\n"
            "        except NotASessionError:\n"
            "            end_ordinal = len(calendar.sessions()) - 1"
        ),
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "w3 a visible session that is not a calendar session has no"
            " ordinal and no window: re-anchoring it to the LAST session"
            " stamps a median over a window the caller never named"
        ),
    ),
    dict(
        id="M277-w3-loader-number-close-laundered",
        owner="test_load_spot_proxy_v2_refuses_everything_else",
        file="src/tree_options/data/vwap_pit_surface.py",
        anchor='            close, volume = cell["close"], cell["volume"]',
        replacement=(
            "            close, volume = (\n"
            '                cell["close"] if isinstance(cell["close"], str) else str(cell["close"]),\n'
            '                cell["volume"],\n'
            "            )"
        ),
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "w3 a JSON NUMBER close is refused outright (loads_exact hands the"
            " loader a Decimal, and the token-form gate exists precisely"
            " because a number means the exactness discipline was bypassed"
            " upstream): re-stringifying the number at the cell read launders"
            " it past the gate and into the dollar-volume term"
        ),
    ),
    dict(
        id="M278-w3-loader-volume-gate-gutted",
        owner="test_load_spot_proxy_v2_refuses_everything_else",
        file="src/tree_options/data/vwap_pit_surface.py",
        # (R4-P2 re-pin) the loader's row validation moved into the ONE
        # shared helper `_validated_spot_v2_row` (loader + constructor copy
        # loop) — same gate, same invariant, now guarding BOTH entry paths
        anchor="    if type(volume) is not int or volume < 0:",
        replacement="    if False:",
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "w3 volume is a STRICT int >= 0 (bools, strings, and negatives"
            " all refuse): gutting the gate lets true coerce to 1 and a"
            " negative count corrupt the dollar-volume product"
        ),
    ),
    dict(
        id="M279-w3-loader-extra-key-tolerated",
        owner="test_load_spot_proxy_v2_refuses_everything_else",
        file="src/tree_options/data/vwap_pit_surface.py",
        anchor='            if not isinstance(cell, dict) or set(cell) != {"close", "volume"}:',
        replacement=(
            '            if not isinstance(cell, dict) or not {"close", "volume"} <= set(cell):'
        ),
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "w3 each session object carries EXACTLY close+volume: tolerating"
            " extra keys lets an undeclared field ride the declared input"
            " un-parsed and un-refused"
        ),
    ),
    dict(
        id="M280-w3-loader-non-iso-skipped",
        owner="test_load_spot_proxy_v2_refuses_everything_else",
        file="src/tree_options/data/vwap_pit_surface.py",
        anchor=(
            "            except ValueError as exc:\n"
            "                raise MassiveOverlayError(\n"
            '                    f"{where}: key {raw_session!r} is not an ISO date"\n'
            "                ) from exc"
        ),
        replacement=("            except ValueError:\n                continue"),
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "w3 a non-ISO session key is REFUSED by name, never silently"
            " skipped: dropping rows hides capture corruption and under-counts"
            " the window the median claims to cover"
        ),
    ),
    dict(
        id="M281-w2-earnings-false-laundered",
        owner="test_candidate_snapshot_earnings_is_the_honest_no_evidence_encoding",
        file="src/tree_options/data/vwap_pit_surface.py",
        anchor="        spans_earnings: AsOf | None = None",
        replacement="        spans_earnings: AsOf | None = AsOf(value=False, available_at=received)",
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "w2 (theory-panel P0-2 ruling (ii)) this lane carries no earnings"
            " calendar, so AsOf(False) LAUNDERS a vendor-stamped PASS"
            " 'no spanning earnings' no source supports; the honest encoding"
            " is None and the 0.2.1 rule answers NOT_EVALUABLE 'missing'"
        ),
    ),
    dict(
        id="M282-w2-earnings-true-stamped",
        owner="test_candidate_snapshot_earnings_is_the_honest_no_evidence_encoding",
        file="src/tree_options/data/vwap_pit_surface.py",
        anchor="        spans_earnings: AsOf | None = None",
        replacement="        spans_earnings: AsOf | None = AsOf(value=True, available_at=received)",
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "w2 the symmetric fabrication: stamping AsOf(True) invents an"
            " earnings-span fact with no evidence either — the snapshot must"
            " carry no earnings claim at all on this lane"
        ),
    ),
    dict(
        id="M283-w7a-drop-incoherence-laundered",
        owner="test_dropped_term_with_a_supplied_value_is_regime_incoherent",
        file="src/tree_options/candidates/filters.py",
        anchor="            and snap.underlying_20d_median_dollar_volume is not None",
        replacement="            and snap.underlying_20d_median_dollar_volume is None",
        selectors=[f"{U}/test_candidate_volume_flow.py"],
        invariant=(
            "w7a the disclosure may not paper over real inputs: a protocol"
            " that drops the underlying-liquidity term while the snapshot"
            " SUPPLIES a value is incoherent and answers NOT_EVALUABLE with"
            " the value withheld — inverting the supplied-check launders the"
            " contradiction into NOT_APPLICABLE-with-value"
        ),
    ),
    dict(
        id="M284-w7a-unknown-term-accepted",
        owner="test_unknown_term_token_refuses_at_the_constructor",
        file="src/tree_options/candidates/filters.py",
        anchor=(
            '        if underlying_liquidity_term not in {"evaluated",'
            ' "dropped_no_equity_aggregates"}:'
        ),
        replacement="        if False:",
        selectors=[f"{U}/test_candidate_volume_flow.py"],
        invariant=(
            "w7a the declared disposition is a two-token Literal: an unknown"
            " underlying_liquidity_term refuses at the constructor exactly as"
            " the regime name does — accepting it lets an unratified"
            " disposition silently behave as 'evaluated'"
        ),
    ),
    dict(
        id="M285-g5-preimage-fields-swapped",
        owner="test_the_preimage_contract_is_pinned",
        file="src/tree_options/trials/null_score.py",
        anchor=(
            '    preimage = _UNIT.join((seed, session.isoformat(), security_id)).encode("utf-8")'
        ),
        replacement=(
            '    preimage = _UNIT.join((seed, security_id, session.isoformat())).encode("utf-8")'
        ),
        selectors=[f"{U}/test_null_score.py"],
        invariant=(
            "G5 the score's preimage is sha256 over seed UNIT session ISO"
            " UNIT security_id in THAT field order: swapping two fields"
            " silently re-scores every cross-section while still looking"
            " deterministic (the owner pins the digest from first principles)"
        ),
    ),
    dict(
        id="M286-g5-interval-bound-broken",
        owner="test_scores_live_in_the_unit_interval",
        file="src/tree_options/trials/null_score.py",
        # (P3-7 re-pin) the mapping moved into the pure helper _unit_score
        # (top 53 bits over 2**53); the SAME mutant — take a ninth byte —
        # keeps the 53-bit denominator and every score lands at or above 1.0
        anchor='    return _unit_score(int.from_bytes(digest[:8], "big"))',
        replacement='    return _unit_score(int.from_bytes(digest[:9], "big"))',
        selectors=[f"{U}/test_null_score.py"],
        invariant=(
            "G5 the leading 64 bits map onto [0, 1) exactly: taking"
            " a ninth byte keeps the same denominator and every score lands"
            " at or above 1.0 — the bound the quintile cut and every"
            " comparison against score thresholds rests on"
        ),
    ),
    dict(
        id="M287-g5-seed-not-hashed",
        owner="test_score_seed_rides_the_config_hash",
        file="src/tree_options/trials/options_run.py",
        anchor=(
            "        # G5: the null-score generator's REQUIRED seed is a first-class\n"
            "        # config key — the declared score model's input rides the hash\n"
            '        **({"score_seed": score_seed} if score_seed is not None else {}),'
        ),
        replacement=(
            "        # G5: the null-score generator's REQUIRED seed is a first-class\n"
            "        # config key — the declared score model's input rides the hash\n"
            "        **{},"
        ),
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "G5 the score seed is a first-class config key: dropping it from"
            " the hashed config lets one null trial masquerade as another"
            " configuration (the payload stamp alone discloses nothing the"
            " registry compares)"
        ),
    ),
    dict(
        id="M288-g1-publication-wall-gutted",
        owner="test_publication_wall_governs_visibility",
        file="src/tree_options/data/vwap_pit_surface.py",
        anchor="            if self._overlay.publication_of(session) <= as_of:",
        replacement="            if True:",
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "G1 the adapter's own publication gate: only sessions whose T+1"
            " 09:00 ET wall has passed are visible — gutting it hands every"
            " read (mark, delta, fill stream, spot) the newest session's bar"
            " at a close(t) decision, the exact t-1 recency leak the wall"
            " exists to prevent"
        ),
    ),
    dict(
        id="M289-g1-participation-key-collapsed",
        owner="test_each_bar_session_carries_its_own_participation_capacity",
        file="src/tree_options/guards/fills.py",
        anchor="            participation_key = (selected.contract_id, selected.session)",
        replacement="            participation_key = selected.contract_id",
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "G1 (via the lane-2 adapter) participation is cumulative per"
            " (contract, BAR SESSION): collapsing the key to the contract"
            " alone starves every session after the first of the volume the"
            " later bars actually traded (M176 pins the cumulative READ"
            " within one bar; this pins the key's second dimension)"
        ),
    ),
    # ---- real-lane round-1 remediation (P1-4/P1-1/P1-3/P1-2/P2-5/P2-6/P3-7,
    # M290-M300) --------------------------------------------------------------
    dict(
        id="M290-p14-exclusion-dropped",
        # (0.2.2 flip re-pin, 2026-08-28) the owner moved OFF the live-yaml
        # pin test: the strip is version-gated OFF at 0.2.2, so the live
        # 0.2.2 yaml can no longer exercise it (the landed-pin test passes
        # under this mutant). The owner now sits on the path the new state
        # does not cover: the pinned 0.2.1 FIXTURE — a real pre-flip file
        # through the real loader, where neutering the exclusion re-hashes
        # it off the cfafc884… pin. Same seam, same selectors file.
        owner="test_the_pinned_021_fixture_hashes_to_the_pre_flip_identity",
        file="src/tree_options/protocol/loader.py",
        anchor=(
            '    if isinstance(liquidity, dict) and liquidity.get("underlying_liquidity_term")'
            ' == "evaluated":'
        ),
        replacement=(
            '    if isinstance(liquidity, dict) and liquidity.get("underlying_liquidity_term")'
            ' == "__never__":'
        ),
        selectors=[f"{U}/test_protocol_loader.py"],
        invariant=(
            "P1-4 the canonical hash represents what the yaml DECLARES: the"
            " defaulted-but-undeclared underlying_liquidity_term must NOT"
            " ride the 0.2.1 identity — stopping the exclusion re-hashes the"
            " untouched 0.2.1 protocol (3b0b8a85…) and breaks the"
            " ledger-bound pin, which the pinned 0.2.1 fixture still"
            " carries post-flip"
        ),
    ),
    dict(
        id="M291-p14-exclusion-always",
        owner="test_a_declared_dropped_term_rides_the_hash",
        file="src/tree_options/protocol/loader.py",
        anchor=(
            '    if isinstance(liquidity, dict) and liquidity.get("underlying_liquidity_term")'
            ' == "evaluated":'
        ),
        replacement=(
            '    if isinstance(liquidity, dict) and "underlying_liquidity_term" in liquidity:'
        ),
        selectors=[f"{U}/test_protocol_loader.py"],
        invariant=(
            "P1-4 the exclusion must not swallow a real DECLARATION: dropping"
            " the key unconditionally makes a declared"
            " dropped_no_equity_aggregates model hash identically to the"
            " default — the disposition is a semantic protocol fact"
        ),
    ),
    dict(
        id="M292-p13-null-seed-refusal-dropped",
        owner="test_null_family_without_a_seed_refuses_before_registration",
        file="src/tree_options/trials/options_run.py",
        anchor="    if model_family == NULL_SCORE_MODEL_FAMILY and score_seed is None:",
        replacement="    if False:",
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "P1-3 a null-sha256 trial must DECLARE its seed: dropping the"
            " refusal lets an undeclared seed register silently — unregistered"
            " randomness under a deterministic-looking model family"
        ),
    ),
    dict(
        id="M293-p13-null-seed-verification-skipped",
        owner="test_null_family_with_a_misstated_seed_refuses_by_name",
        file="src/tree_options/trials/options_run.py",
        anchor="    if model_family == NULL_SCORE_MODEL_FAMILY and score_seed is not None:",
        replacement="    if False:",
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "P1-3 the stamped seed is VERIFIED against every scored row:"
            " skipping the recompute lets a misstated seed masquerade as the"
            " declared score model (two T-NULL trials, one identity)"
        ),
    ),
    dict(
        id="M294-p12-exchange-window-falls-back-to-overlay",
        owner="test_exchange_session_missing_from_every_capture_fails_closed",
        file="src/tree_options/data/vwap_pit_surface.py",
        anchor="        calendar = self._exchange_calendar",
        replacement="        calendar = self._overlay.calendar",
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "P1-2 the 20-session liquidity window is contiguous on the"
            " EXCHANGE calendar: falling back to the overlay's union-of-"
            " captures calendar lets a market session missing from every"
            " capture self-certify as contiguity and the median PASS where"
            " the design says fail-closed (the Jan-16 scenario)"
        ),
    ),
    dict(
        id="M295-p25-finiteness-gate-removed",
        owner="test_non_finite_closes_refuse_cleanly",
        file="src/tree_options/data/vwap_pit_surface.py",
        # (R4-P2 re-pin) the finiteness gate moved into the ONE shared
        # helper `_validated_spot_v2_row` — same gate, same invariant, now
        # guarding the constructor's copy loop too (the R4-P2 defect was
        # exactly this gate existing ONLY at the file gate)
        anchor="    if not close_value.is_finite() or close_value <= 0:",
        replacement="    if close_value <= 0:",
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "P2-5 'Infinity' is POSITIVE-looking: without the finiteness"
            " gate it loads, and an infinity median flips the liquidity rule"
            " to PASS — fail-open on a non-finite declared input"
        ),
    ),
    dict(
        id="M296-p26-disclosure-scope-fields-voided",
        owner="test_holdout_seal_block_states_its_declared_scope_and_the_artifacts_lane",
        file="src/tree_options/trials/options_run.py",
        anchor=(
            '        applied = f"unconditional-refusal (declared scope: {FINAL_HOLDOUT_SCOPE})"'
        ),
        replacement='        applied = ""',
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "P2-6 the holdout_seal block must state HOW the seal was applied"
            " (unconditional refusal under a lane-2 DECLARED scope): voiding"
            " the field lets a lane-1 artifact read as claiming a"
            " lane-1-scoped seal again"
        ),
    ),
    dict(
        id="M297-p37-mapping-reverts-to-64-bit-float-division",
        owner="test_maximal_digest_stays_strictly_below_one",
        file="src/tree_options/trials/null_score.py",
        anchor="    return (leading_bits >> _SHIFT) / _DENOMINATOR",
        replacement="    return leading_bits / float(2**64)",
        selectors=[f"{U}/test_null_score.py"],
        invariant=(
            "P3-7 the mapping must be STRICTLY below 1.0 for every input:"
            " reverting to the 64-bit float division rounds the maximal"
            " prefixes to exactly 1.0, violating the declared [0, 1)"
        ),
    ),
    dict(
        id="M298-p11-fill-engine-receives-the-grid-calendar",
        owner="test_dual_calendar_friday_grid_daily_bars_fills",
        file="src/tree_options/backtest/options.py",
        anchor="        fill_calendar,",
        replacement="        calendar,",
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "P1-1 the fill engine carries the EXECUTION calendar: handing it"
            " the Friday-only decision grid rejects the adapter's daily bar"
            " (BAR_SESSION_NOT_IN_CALENDAR / BAR_NOT_MOST_RECENT) — the"
            " ratified real lane cannot fill"
        ),
    ),
    dict(
        id="M299-p11-splitter-receives-the-execution-calendar",
        owner="test_dual_calendar_ruled_geometry_yields_the_era_folds",
        file="src/tree_options/trials/options_run.py",
        anchor="    splitter = WalkForwardSplitter(\n        calendar,",
        replacement="    splitter = WalkForwardSplitter(\n        execution_calendar or calendar,",
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "P1-1 the DECISION GRID drives splitting: enumerating folds on"
            " the daily execution calendar makes every fold's 13 consecutive"
            " daily test sessions fall outside a Fridays-only world set —"
            " the 'no folds' horn of the single-calendar defect"
        ),
    ),
    dict(
        id="M300-p11-dual-calendar-disclosure-voided",
        owner="test_dual_calendar_ruled_geometry_yields_the_era_folds",
        file="src/tree_options/trials/options_run.py",
        anchor='        payload["decision_calendar"] = _calendar_descriptor(calendar)',
        replacement='        payload["decision_calendar"] = None',
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "P1-1 both calendar identities are DISCLOSED in the payload: an"
            " artifact whose decision_calendar descriptor is voided can no"
            " longer name which grid split its folds and which calendar its"
            " fills ran on"
        ),
    ),
    # ---- remediation wave 2 (R2-P1-a/b/c + R2-P2-d + the no-op repair,
    # M301-M306) -----------------------------------------------------------------
    dict(
        id="M301-r2a-constructor-gate-gutted",
        owner="test_an_unbound_exchange_calendar_refuses_at_construction",
        file="src/tree_options/data/vwap_pit_surface.py",
        anchor="            if supplied_identity != REPO_EXCHANGE_CALENDAR_CONTENT_SHA256:",
        replacement="            if False:",
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "R2-P1-a the exchange authority is PROVENANCE-BOUND at the"
            " constructor: gutting the content-identity gate re-accepts ANY"
            " SessionCalendar — including the overlay's union-of-captures"
            " calendar, the exact self-certification vector round-1 P1-2"
            " closed, handed back in through the parameter"
        ),
    ),
    dict(
        id="M302-r2a-factory-pin-gutted",
        owner="test_the_bound_factory_refuses_a_checksum_consistent_doctored_fixture",
        file="src/tree_options/data/vwap_pit_surface.py",
        anchor="    if bound_identity != REPO_EXCHANGE_CALENDAR_CONTENT_SHA256:",
        replacement="    if False:",
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "R2-P1-a the factory's pin binds SEMANTICS, not file bytes:"
            " gutting it lets a doctored fixture whose sidecar checksum was"
            " REGENERATED (checksum-disciplined but content-different) load"
            " silently as the exchange authority"
        ),
    ),
    dict(
        id="M303-r2b-descriptor-content-hash-voided",
        owner="test_a_calendar_differing_by_one_interior_session_is_a_different_trial_identity",
        file="src/tree_options/trials/options_run.py",
        anchor='        "content_sha256": calendar_content_sha256(calendar),',
        replacement='        "content_sha256": "0" * 64,',
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "R2-P1-b the calendar descriptor's content hash is what makes"
            " one interior session (or one early close) a trial-identity"
            " change: voiding it to a constant returns to the lossy"
            " {name, count, first, last} identity INV-14 refused to stamp"
        ),
    ),
    dict(
        id="M304-r2c-decision-seam-falls-back-to-overlay",
        owner="test_decision_at_on_an_early_close_grid_session_is_the_true_close",
        file="src/tree_options/data/vwap_pit_surface.py",
        anchor="        decision_at = self.decision_close(decision_session)",
        replacement=(
            "        decision_at = self._overlay.calendar.session_close(decision_session)"
        ),
        selectors=[f"{U}/test_trials_options_run.py", f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "R2-P1-c the decision instant comes from the DECISION grid"
            " (early-close aware), never silently from the overlay's"
            " nominal 16:00: falling back re-opens the 13:00/16:00"
            " incoherence that made no consistent configuration exist."
            " (R3-P1-2 re-pin: the inline R2 seam became the surface-wide"
            " decision_close() helper, so the anchor moved to its"
            " candidate_snapshot call site — same invariant, same owner)"
        ),
    ),
    dict(
        id="M305-r2d-dropped-term-sentinel-supplied",
        owner="test_a_dropped_liquidity_term_supplies_absence_through_the_adapter",
        file="src/tree_options/data/vwap_pit_surface.py",
        anchor="            dollar_volume: AsOf | None = None",
        replacement=(
            "            dollar_volume: AsOf | None = AsOf(\n"
            "                value=self._overlay.median_dollar_volume(underlying, session),\n"
            "                available_at=received,\n"
            "            )"
        ),
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "R2-P2-d a DROPPED liquidity term supplies ABSENCE: minting the"
            " overlay sentinel into a dropped regime makes the filter judge"
            " the snapshot regime-incoherent (NOT_EVALUABLE) and the ruled"
            " 0.2.2 NOT_APPLICABLE disclosure unreachable through the"
            " adapter again"
        ),
    ),
    dict(
        id="M306-r2e-fold-test-window-end-disclosure-voided",
        owner="test_dual_calendar_ruled_geometry_yields_the_era_folds",
        file="src/tree_options/trials/options_run.py",
        anchor='                    "end": test_window[-1].isoformat(),',
        replacement='                    "end": test_window[0].isoformat(),',
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "R2 fix 5 the per-fold test-window END is real disclosed"
            " geometry (Good Friday 2026-04-03 absent makes 2026-05-01 the"
            " 13th session): stamping start-as-end is exactly the wrong"
            " quarter-3 end-date claim the old no-op assertion let slip"
            " through review"
        ),
    ),
    # ---- remediation wave 3 (R3-P1-1 + R3-P1-2, M307-M309) ----------------------
    dict(
        id="M307-r3a-calendar-class-identity-laundered",
        owner="test_a_subclassed_exchange_calendar_with_identical_data_refuses",
        file="src/tree_options/time/calendar.py",
        anchor=(
            '            "calendar_class": f"{type(calendar).__module__}'
            '.{type(calendar).__qualname__}",'
        ),
        replacement=(
            '            "calendar_class": "tree_options.time.calendar.StaticSessionCalendar",'
        ),
        selectors=[f"{U}/test_vwap_pit_surface.py", f"{U}/test_trials_options_run.py"],
        invariant=(
            "R3-P1-1 the digest names WHO computed the calendar, not just"
            " WHAT it reports: laundering the class identity to a constant"
            " lets a StaticSessionCalendar SUBCLASS over the canonical data"
            " — free to override ordinal()/session_close() (the Codex probe"
            " moved a liquidity median across the $50M threshold) — retain"
            " the pinned digest and pass the constructor gate, and lets the"
            " same subclass ride the trial descriptors as the base class's"
            " identity"
        ),
    ),
    dict(
        id="M308-r3b-strategy-decision-instant-falls-back-to-overlay",
        owner="test_an_action_published_after_the_true_close_is_not_yet_known",
        file="src/tree_options/options/strategy.py",
        anchor=(
            "    decision_at = surface.decision_close(decision_session)\n"
            "    eligible = frozenset(surface.eligible_as_of(decision_session))"
        ),
        replacement=(
            "    decision_at = surface.overlay.calendar.session_close(decision_session)\n"
            "    eligible = frozenset(surface.eligible_as_of(decision_session))"
        ),
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "R3-P1-2 build_candidates derives THE decision instant from the"
            " surface's decision_close seam: reverting to the overlay"
            " (execution) calendar's nominal 16:00 makes a pending action"
            " published 13:00-16:00 on an early-close session count as"
            " KNOWN at decision, so the name is excluded on future"
            " information (INV-02) and the audit stamp reads"
            " excluded_pending_action instead of the honest refusal"
        ),
    ),
    dict(
        id="M309-r3c-decision-close-seam-ignores-the-decision-calendar",
        owner="test_an_action_published_after_the_true_close_is_not_yet_known",
        file="src/tree_options/data/vwap_pit_surface.py",
        anchor=(
            "        if self._decision_calendar is not None:\n"
            "            return self._decision_calendar.session_close(decision_session)\n"
            "        return self._overlay.calendar.session_close(decision_session)"
        ),
        replacement="        return self._overlay.calendar.session_close(decision_session)",
        selectors=[f"{U}/test_vwap_pit_surface.py", f"{U}/test_trials_options_run.py"],
        invariant=(
            "R3-P1-2 the seam must HONOR a supplied decision calendar:"
            " ignoring it answers the overlay's nominal 16:00 on the"
            " early-close sessions, which both breaks the snapshot"
            " coherence the R2 seam existed for and re-opens the"
            " future-information exclusion in candidate construction"
        ),
    ),
    # ---- remediation wave 4 (R4-P1 + R4-P2, M310-M312) ----------------------------
    dict(
        id="M310-r4a-unwired-surface-boundary-gutted",
        owner="test_an_unwired_surface_refuses_a_grid_stamped_trial_before_registration",
        file="src/tree_options/trials/options_run.py",
        anchor="    if surface_identity != stamped_identity:",
        replacement="    if False:",
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "R4-P1 the trial boundary BINDS the surface's disclosed"
            " decision-calendar authority to the stamped calendar: gutting"
            " the identity refusal lets an UNWIRED VwapPitSurface (the"
            " overlay's nominal 16:00) run under an era-grid-stamped trial"
            " — deciding at 16:00 on the grid's 13:00 early-close sessions,"
            " different counters under the same declared configuration"
            " (INV-02/INV-14 at the trial boundary)"
        ),
    ),
    dict(
        id="M311-r4b-boundary-compares-names-not-content-identity",
        owner="test_a_subclassed_decision_calendar_is_a_different_trial_identity",
        file="src/tree_options/trials/options_run.py",
        anchor=(
            "        disclosed = surface.decision_calendar\n"
            "        surface_identity = calendar_content_sha256(disclosed)\n"
            "        stamped_identity = calendar_content_sha256(calendar)"
        ),
        replacement=(
            "        disclosed = surface.decision_calendar\n"
            '        surface_identity = getattr(disclosed, "name", type(disclosed).__name__)\n'
            '        stamped_identity = getattr(calendar, "name", type(calendar).__name__)'
        ),
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "R4-P1 the bind rides the COMPLETE content identity"
            " (calendar_content_sha256), never a lossy label: the twin"
            " decision grid — a StaticSessionCalendar SUBCLASS over the"
            " identical committed fixture — carries the SAME name, the same"
            " sessions and the same early closes, so a name comparison"
            " lets the unwired surface run under a grid that differs only"
            " in WHO computes it (the R3-P1-1 class identity), exactly the"
            " stamping a content digest exists to refuse"
        ),
    ),
    dict(
        id="M312-r4c-spot-v2-constructor-validation-removed",
        owner="test_the_spot_v2_constructor_refuses_invalid_rows",
        file="src/tree_options/data/vwap_pit_surface.py",
        anchor=(
            "                rows[session] = _validated_spot_v2_row(where, session, close, volume)"
        ),
        replacement="                rows[session] = (close, volume)",
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "R4-P2 constructor validation parity: bypassing the shared row"
            " discipline returns to the bare dict(sessions) copy — an"
            " injected Decimal('Infinity') close is POSITIVE-looking, the"
            " median path stamps an infinite vendor value, and the"
            " liquidity comparison falls through to PASS (fail-open)"
        ),
    ),
    dict(
        id="M313-r5a-behavioral-equality-dropped",
        owner="test_a_wrong_instant_surface_refuses_naming_both_instants",
        file="src/tree_options/trials/options_run.py",
        anchor="        if surface_close != declared_close:",
        replacement="        if False:",
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "R5-P1 the M310/M311 gap Codex round 5 reproduced: the digest"
            " bind certifies the DISCLOSED calendar, but the decisions call"
            " decision_close() — a subclass overriding only"
            " decision_calendar passes the digest while its method answers"
            " from another calendar. The behavioral equality is what"
            " catches it. Owner is the WRONG-INSTANT twin (the surface"
            " answers every decision session, at 16:00 where the stamped"
            " grid closes 13:00): the digest passes and decision_close()"
            " never raises, so under this mutant the lying surface"
            " registers and runs — only the equality kills it. The"
            " era-overlay lying probe (first decision session outside the"
            " capture span) deliberately still refuses under this mutant"
            " via the cannot-answer branch — the twin is the no-masking"
            " scenario, exactly as M311's twin was"
        ),
    ),
    dict(
        id="M314-r5b-spot-finiteness-gate-removed",
        owner="test_the_spot_constructor_refuses_non_finite_spots",
        file="src/tree_options/data/spot_token.py",
        anchor="    if not spot.is_finite():",
        replacement="    if False:",
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "R5-P2 the ORDINARY spot path's shared finiteness gate"
            " (_validated_spot_token, called at BOTH entry points —"
            " _load_spot's file path and the adapter's constructor copy"
            " loop): without it Decimal('Infinity') is POSITIVE-looking,"
            " passes the <= 0 gate, loads/copies unchanged, and an"
            " infinite spot flows into intrinsic -> the election policy"
            " where any finite bid is below Infinity * 0.98 (a forced"
            " early-exercise election on malformed input); NaN then"
            " escapes as a raw decimal.InvalidOperation. Killing the"
            " shared gate fails BOTH refusals (constructor owner here;"
            " the loader's Infinity test fails under the same mutant —"
            " verified in reallane-r5-mutantM314-red.log). R6-P2 moved"
            " the gate's body to tree_options/data/spot_token.py (the ONE"
            " contract the census scripts now share); same anchor, same"
            " killers — massive_overlay._validated_spot_token is a thin"
            " rebranding wrapper around it"
        ),
    ),
    dict(
        id="M315-r6a-frozen-map-bypass",
        owner="test_a_stateful_lying_surface_runs_identically_to_the_wired_surface",
        file="src/tree_options/trials/options_run.py",
        anchor="            return decision_closes[decision_session]",
        replacement=("            return underlying.decision_close(decision_session)"),
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "R6-P1 the frozen-map bypass: the bound decision_close falls"
            " back to the underlying surface's OVERRIDABLE method — exactly"
            " the pre-R6 runtime, where the boundary verified the method"
            " once per session and the run then consulted it again. The"
            " stateful probe (first call right, later calls 16:00) passes"
            " every boundary guard, so under this mutant the lying trial's"
            " candidate snapshot on the early-close Friday 2025-11-28 is"
            " stamped 21:00Z, the filter answers decision_coherence"
            " NOT_EVALUABLE, and the counters/artifact DIVERGE from the"
            " correctly-wired run under one declared configuration — only"
            " the identity assertion kills it. The bind's other seams"
            " (unmapped-session refusal, same-class construction) are"
            " deliberately NOT the owner: honest surfaces behave"
            " identically under the bypass, so no other test moves."
            " (R7-P2 re-pin: the R6 wrapper's"
            " self._decision_closes[...] return became the factory"
            " closure's return — same mutant, same owner, same kill; the"
            " closure's enclosing scope carries `underlying`, which is"
            " what the bypass replacement delegates to)"
        ),
    ),
    dict(
        id="M316-r6b-census-spot-validation-dropped",
        owner="test_the_census_spot_loader_refuses_non_finite_spots",
        file="scripts/inspect_structural_coverage.py",
        anchor=(
            "    return validated_spot_token(where, session, value, refuse=StructuralCoverageError)"
        ),
        replacement="    return _dec(value, where)",
        selectors=[f"{U}/test_inspect_structural_coverage.py"],
        invariant=(
            "R6-P2 the census loader's validation dropped — the return to"
            " the SECOND contract: _dec accepts 'Infinity' and the old"
            " <= 0-only gate let POSITIVE infinity LOAD, so an"
            " explicit-date infinite value satisfied the census's"
            " presence-only check, classify_pair answered COMPLETE, and a"
            " malformed capture exited 0. The shared-validator delegation"
            " is the entire fix on this side; gutting it re-opens the"
            " probe (the owner fails on BOTH parameters — Infinity loads,"
            " NaN escapes as a raw decimal.InvalidOperation — and the"
            " census-side exit-2 test fails under the same mutant)"
        ),
    ),
    dict(
        id="M317-r6c-flat-form-presence-reverted",
        owner="test_a_flat_form_spot_proxy_covers_every_session_friday",
        file="scripts/build_coverage_census.py",
        anchor=(
            "            spot_present = friday_date in spot_sessions"
            " or SPOT_SENTINEL_SESSION in spot_sessions"
        ),
        replacement="            spot_present = friday_date in spot_sessions",
        selectors=[f"{U}/test_coverage_census.py"],
        invariant=(
            "R6-P2 the flat-form presence check reverted to exact-Friday"
            " membership only: the loader stores the documented"
            ' all-session FLAT form ({"SPY": "600.00"}) under the'
            " date.min sentinel, so a valid flat proxy marks every"
            " session Friday SPOT_MISSING_SESSION and the census exits 5"
            " — the exact reproduced defect. The sentinel disjunct is the"
            " whole fix; the pinned-bytes and manifest guards still pass"
            " on this capture (no masking), so only the flat-form census"
            " kills it"
        ),
    ),
    dict(
        id="M318-r7a-freeze-rereads-the-calendar",
        owner="test_the_freeze_consumes_the_verified_instant_never_a_third_calendar_read",
        file="src/tree_options/trials/options_run.py",
        anchor="        decision_closes[session] = declared_close",
        replacement="        decision_closes[session] = calendar.session_close(session)",
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "R7-P1 the freeze re-reads the stamped calendar instead of"
            " storing the value the boundary actually compared: the loop's"
            " own declared_close is DISCARDED and a SECOND, unverified"
            " calendar.session_close(session) rebuilds the map — the"
            " pre-R7 construction, where a mutable calendar answering the"
            " fixture close on a session's first two reads and 16:00"
            " thereafter passed the entire preflight (the digest hashes"
            " sessions + early closes + the class, never per-session"
            " method state) while the THIRD read froze the wrong instant."
            " The mutable-calendar probe passes every other guard — it"
            " discloses itself, answers both preflight reads with the"
            " fixture close, never raises — so the call count on the"
            " pre-first-scored sessions is the ONLY thing that moves: 3"
            " reads under the mutant, 2 under the fix (no masking)"
        ),
    ),
    dict(
        id="M319-r7b-bind-drops-the-instance-attribute-override",
        owner="test_a_subclass_override_resolves_the_frozen_decision_close",
        file="src/tree_options/trials/options_run.py",
        anchor="        bound.decision_close = decision_close  # type: ignore[method-assign]",
        replacement="        pass",
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "R7-P2 the bind drops the instance-attribute override: the"
            " bound instance keeps the CLASS's decision_close, so a"
            " subclass override of another method that internally calls"
            " self.decision_close() reaches the UNDERLYING's overridable"
            " method instead of the frozen map — the second horn of the"
            " R6 wrapper's __getattr__ delegation, re-expressed on the"
            " same-class bind (the copied __dict__ carries the liar's"
            " shared _answered set, already consumed by the boundary's"
            " one-per-session read, so the class method answers 16:00)."
            " The probing surface passes every boundary guard — digest"
            " binds, first call per session answers the stamped close —"
            " so only the recorded instants move: every probe reads the"
            " no-early-close twin's close, the early-close Friday"
            " 2025-11-28 among them (no masking). (R8-P2 re-pin: the"
            " assignment moved inside the install-verification try block,"
            " so the anchor re-indented from 4 to 8 spaces — same mutant,"
            " same owner, same kill; NOTE the kill is now DOUBLE-OWNED:"
            " the read-back verification co-catches the dropped install"
            " with its named refusal BEFORE the delegation probes can"
            " record, so the owner fails on the refusal — the owner still"
            " fails BY NAME, and with the verification itself removed"
            " (M320-r8a) this mutant's kill reverts to the delegation"
            " probes alone)"
        ),
    ),
    # ---- remediation wave 8 (R8-P1 + R8-P2, M320-M322) ---------------------------
    dict(
        id="M320-r8a-bind-install-verification-dropped",
        owner="test_a_setattr_swallowing_class_is_still_refused_by_the_read_back",
        file="src/tree_options/trials/options_run.py",
        anchor="    if bound.decision_close is not decision_close:",
        replacement="    if False:",
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "R8-P2 the install verification dropped: the bind assigns the"
            " frozen closure and never reads it back, so the assignment"
            " can land in the void and the run proceeds on the class's"
            " overridable method — the pre-R8-P2 state. (R9-P2 re-pin:"
            " the previous owner — the descriptor-surface test — is now"
            " refused BEFORE the install by the MRO pre-scan"
            " (_refuse_descriptor_seams), which MASKS the dropped"
            " read-back for every named-descriptor shape; the new owner's"
            " __setattr__-swallowing class is the shape the scan cannot"
            " name, so ONLY the read-back catches it — the mutant's kill"
            " is unique and unmasked). The swallow probe passes every"
            " other boundary guard — no seam descriptor, no"
            " __getattribute__, digest and behavioral equality bind — so"
            " only the missing read-back moves: the trial registers and"
            " completes (no masking; the setter-less and __slots__ horns"
            " keep their own owners)"
        ),
    ),
    dict(
        id="M321-r8b-execute-fed-the-original-calendar",
        owner="test_a_fourth_read_calendar_liar_runs_identically_to_the_honest_calendar",
        file="src/tree_options/trials/options_run.py",
        anchor="            calendar=bound_calendar,",
        replacement="            calendar=calendar,",
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "R8-P1 the runtime calendar bind dropped: _execute receives the"
            " caller's ORIGINAL mutable calendar instead of the bound one,"
            " so every runtime session_close site — the filter's coherence"
            " read, plan_orders' entry stamp, plan_exit_order, the"
            " retry/forced/decided sell stamps, the close(t) mark, the"
            " fill doors — re-reads the overridable method: the pre-R8"
            " state, where the fourth-read liar (honest through read 3,"
            " 16:00 from read 4) stamped the early-close session 2018-07-03's"
            " entry 20:00Z under one declared configuration while the"
            " honest run stamped the verified 17:00Z. The probe passes"
            " every boundary guard (it discloses itself and survives the"
            " coherence read), so only the runtime reads move: the liar's"
            " body diverges from the honest run AND its decision sessions"
            " carry 5-24 reads instead of exactly the preflight's two (no"
            " masking; the cancellation-window and fail-closed tests move"
            " under this mutant too, but the owner asserts first)"
        ),
    ),
    dict(
        id="M322-r8c-calendar-closure-unmapped-fallback-removed",
        owner="test_the_bound_calendar_refuses_unmapped_sessions_fail_closed",
        file="src/tree_options/trials/options_run.py",
        anchor="            return frozen_closes[session]",
        replacement="            return calendar.session_close(session)",
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "R8-P1 the calendar closure's fail-closed horn removed: the"
            " frozen map's return delegates to the calendar's own"
            " overridable session_close, so a session outside the frozen"
            " set falls back to the method instead of refusing by name —"
            " exactly the runtime read of an unverified instant the bind"
            " exists to make impossible. The owner's honest-calendar"
            " assertions still pass under the mutant (the delegation"
            " answers the same values for mapped sessions); only the"
            " unmapped horn moves: a non-session date raises"
            " NotASessionError, never the named ValueError (no masking)"
        ),
    ),
    # ---- remediation wave 9 (R9-P1 + R9-P2 + R9-P3, M323-M325) ------------------
    dict(
        id="M323-r9a-same-object-execution-calendar-denormalized",
        owner="test_a_same_object_execution_calendar_is_the_none_form_at_runtime",
        file="src/tree_options/trials/options_run.py",
        anchor=(
            "            execution_calendar=None if execution_calendar is"
            " calendar else execution_calendar,"
        ),
        replacement="            execution_calendar=execution_calendar,",
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "R9-P1 the same-object normalization dropped: _execute receives"
            " the caller's ORIGINAL execution_calendar object even when it"
            " IS the stamped decision calendar, so the backtest routes it"
            " to the fill engine — the pre-R9 state, where"
            " execution_calendar=None and execution_calendar=<the same"
            " object> were stamped and hashed IDENTICALLY yet differed at"
            " runtime: the None form's fills read the BOUND calendar's"
            " frozen instants while the same-object form's doors re-read"
            " the MUTABLE original, whose third-and-later answers (a"
            " calendar honest through the boundary's two reads, 16:00"
            " thereafter) killed the correctly-stamped 13:00 order as"
            " DECISION_INSTANT_NOT_CLOSE (INV-02/INV-14 under one declared"
            " configuration). The probe passes every boundary guard and"
            " the R8 calendar bind alike, so only the fill engine's"
            " unbound read moves: the same-object run's payload diverges"
            " from the None run's and its calendar read counts exceed the"
            " boundary's two (no masking; the dual-calendar lane keeps"
            " its own owners)"
        ),
    ),
    dict(
        id="M324-r9b-descriptor-seam-scan-dropped",
        owner="test_a_stateful_descriptor_surface_is_refused_before_registration",
        file="src/tree_options/trials/options_run.py",
        anchor=(
            "        if seam_attr is not None and any(\n"
            '            "__set__" in seam_type.__dict__ or'
            ' "__delete__" in seam_type.__dict__\n'
            "            for seam_type in type(seam_attr).__mro__\n"
            "        ):"
        ),
        replacement="        if False:",
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "R9-P2 the data-descriptor MRO pre-scan dropped: a class"
            " defining the seam as a data descriptor reaches the install"
            " again, and a STATEFUL descriptor — __set__ accepts the frozen"
            " closure, __get__ returns it for exactly the FIRST read —"
            " passes the R8 read-back (identity holds at the only read it"
            " ever makes) and answers the unfrozen 16:00-lying callable on"
            " every later read: the trial registers and the runtime"
            " consumes the lying callable, the post-close order-stamping"
            " defect reborn. The probe passes every other boundary guard"
            " — digest binds, behavioral equality binds, the install"
            " verification binds — so only the missing class-shape"
            " refusal moves: the trial registers and completes (no"
            " masking; the immediately-swallowing descriptor is co-caught"
            " by the read-back alone, so this owner's stateful descriptor"
            " is the UNIQUE kill for the scan — and the __getattribute__"
            " arm of the same scan keeps its own refusal test)."
            " (round-10 debt-f re-pin: the anchor moved with the code it"
            " pins — the scan now classifies by walking"
            " type(seam_attr).__mro__ class dicts instead of hasattr —"
            " the arm's owner and meaning are unchanged, and the"
            " regression to the OLD classification is its own mutant,"
            " M329-r10f)"
        ),
    ),
    dict(
        id="M325-r9c-dictless-bind-refusal-dropped",
        owner="test_an_empty_slots_no_dict_calendar_refuses_by_name",
        file="src/tree_options/trials/options_run.py",
        anchor='    if not hasattr(bound, "__dict__"):',
        replacement="    if False:",
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "R9-P3 the no-__dict__ refusal dropped: a legal __slots__ = ()"
            " class over no __dict__-bearing base passes the falsy"
            " nonempty-slots scan, is constructed, and hits the"
            " unconditional bound.__dict__.update(...) — the pre-R9"
            " state, where the factory raised a raw AttributeError out of"
            " the boundary instead of a named refusal. The probe is a"
            " well-formed stateless structural calendar, so nothing else"
            " in the boundary moves: only the refusal's absence moves —"
            " the raw AttributeError escapes the factory, never the named"
            " ValueError (no masking; the surface-side twin co-catches"
            " through the shared helper)"
        ),
    ),
    dict(
        id="M326-r9d-getattribute-scan-dropped",
        owner="test_a_getattribute_overriding_surface_is_refused_by_name",
        file="src/tree_options/trials/options_run.py",
        anchor=("        if getter is not None and getter is not object.__getattribute__:"),
        replacement="        if False:",
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "R9-P2 the __getattribute__ arm of the MRO pre-scan dropped: a"
            " class overriding __getattribute__ reaches the install again,"
            " and an instance __getattribute__ override can rewrite ANY"
            " later attribute read — returning the installed closure for"
            " the one verification read and something else for every"
            " runtime read — so the freeze cannot be durably installed on"
            " such a class, yet the bind accepts it (the pre-R9-P2 state,"
            " where no refusal existed for the shape). The neutral-"
            " delegation probe changes no behavior today, so nothing else"
            " in the boundary moves: only the missing refusal moves — the"
            " bind succeeds, DID NOT RAISE. The descriptor arm of the same"
            " scan stays live under this mutant, so every descriptor-shape"
            " test remains green (no masking); the calendar-side twin test"
            " co-catches the same arm through the shared helper — the kill"
            " is DOUBLE-OWNED, exactly as M319's was under its re-pin, and"
            " the owner asserts first)"
        ),
    ),
    # ---- known-debt wave 10 (round-10 debt-e + debt-f + debt-g,
    # M327-M329, plus the M324 re-pin above) -----------------------------------
    dict(
        id="M327-r10e-class-authority-gate-dropped",
        owner="test_a_digest_spoofing_subclass_is_refused_by_the_class_authority",
        file="src/tree_options/data/vwap_pit_surface.py",
        anchor="            if type(exchange_calendar) is not StaticSessionCalendar:",
        replacement="            if False:",
        selectors=[f"{U}/test_vwap_pit_surface.py"],
        invariant=(
            "round-10 debt-e the CLASS authority dropped: the pinned"
            " digest binds the concrete class only through __module__ +"
            " __qualname__ STRINGS, both plain assignable class"
            " attributes, so a subclass that reassigns them to the"
            " canonical values, inherits the committed fixture's sessions"
            " and early closes, and overrides ordinal() FORGES"
            " REPO_EXCHANGE_CALENDAR_CONTENT_SHA256 with no SHA collision"
            " — the content authority ACCEPTS it, and the shifted"
            " 20-session window answers a future-including liquidity"
            " median at the visible session's received time (INV-02/"
            " INV-14 under the pinned identity). The probe passes every"
            " other guard the gate owns (its digest EQUALS the pin —"
            " asserted in-test, so the surviving digest arm cannot catch"
            " it and M301/M307 keep their own owners), and the honest"
            " repo path constructs the BASE class, so only the missing"
            " identity check moves: the forged twin is ACCEPTED, DID NOT"
            " RAISE (no masking; every content-different calendar is"
            " still refused by the digest under this mutant)"
        ),
    ),
    dict(
        id="M328-r10g-dict-type-guard-dropped",
        owner="test_a_noop_dict_property_surface_refuses_by_name",
        file="src/tree_options/trials/options_run.py",
        anchor="    if type(bound.__dict__) is not dict:",
        replacement="    if False:",
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "round-10 debt-g the dict-type requirement dropped: the R9-P3"
            " refusal proved only PRESENCE (hasattr), so a class-level"
            " __dict__ property returning a mapping passes it — the"
            " pre-debt-g state. The owner's NO-OP mapping silently drops"
            " the copied state while the install still lands in the real"
            " per-instance storage (attribute writes never consult the"
            " property), the one-time read-back holds identity, and the"
            " bind returns a state-stripped instance — accepted"
            " silently, failing only later, after registration: the bind"
            " succeeds, DID NOT RAISE. The raising-horn twin (the"
            " calendar-side property whose update throws) is co-caught"
            " under this mutant by the guarded copy's DIFFERENT named"
            " refusal — the kill is DOUBLE-OWNED, documented exactly as"
            " M326's, and the owner asserts first (no masking: the"
            " empty-slots and nonempty-slots refusals keep their own"
            " owners, M325's anchor line untouched above the guard)"
        ),
    ),
    dict(
        id="M329-r10f-descriptor-scan-reverted-to-hasattr",
        owner="test_an_introspection_hiding_descriptor_is_refused_by_name",
        file="src/tree_options/trials/options_run.py",
        anchor=(
            "        if seam_attr is not None and any(\n"
            '            "__set__" in seam_type.__dict__ or'
            ' "__delete__" in seam_type.__dict__\n'
            "            for seam_type in type(seam_attr).__mro__\n"
            "        ):"
        ),
        replacement=(
            "        if seam_attr is not None and (\n"
            '            hasattr(seam_attr, "__set__") or'
            ' hasattr(seam_attr, "__delete__")\n'
            "        ):"
        ),
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "round-10 debt-f the class-dict walk reverted to hasattr"
            " introspection: hasattr consults the descriptor OBJECT's own"
            " overridable __getattribute__, so a hostile descriptor class"
            " that DEFINES __set__ (a real data descriptor under"
            " CPython's protocol, which consults the type's class dicts)"
            " while raising AttributeError for '__set__'/'__delete__' on"
            " introspection hides from the scan, reaches the install,"
            " accepts the frozen closure in __set__, returns it for the"
            " one-time verification read, and answers the unfrozen"
            " 16:00-lying callable on every later read — the trial"
            " registers and the runtime consumes the lying callable. The"
            " probe passes every other boundary guard AND the R8 read-"
            " back (its __get__ holds identity for exactly the first"
            " post-install read — proven in-test on the spent twin), so"
            " only the classification moves: the bind succeeds, DID NOT"
            " RAISE (no masking: every hasattr-VISIBLE descriptor shape —"
            " the property, the temporal-evasion descriptor — is still"
            " refused under the reverted scan, so M324's owner and every"
            " other descriptor-shape test stay green under this mutant;"
            " the __getattribute__ arm is untouched)"
        ),
    ),
    dict(
        id="M330-022-earnings-version-gate-dropped",
        owner="test_the_version_gate_keeps_021_refusals_under_a_disclosed_absence_declaration",
        file="src/tree_options/candidates/filters.py",
        anchor="protocol_version_at_least(protocol.meta.protocol_version, 0, 2, 2)",
        replacement="True",
        selectors=[f"{U}/test_candidate_filters.py"],
        invariant=(
            "022-B the earnings disclosed-absence pass is VERSION-GATED: the"
            " same declared `disclosed_absence` on a protocol still stamped"
            " 0.2.1 must keep the honest dark lane (NOT_EVALUABLE) — the"
            " version bump is what turns the lane on, and dropping the gate"
            " activates the 0.2.2 pass one version early"
        ),
    ),
    dict(
        id="M331-022-disclosed-row-dropped",
        owner="test_a_022_declared_run_passes_candidates_that_021_refuses",
        file="src/tree_options/candidates/filters.py",
        anchor="elif snap.spans_earnings is None and self.earnings_disclosed_absence:",
        replacement="elif False:",
        selectors=[f"{U}/test_candidate_filters.py"],
        invariant=(
            "022-B the disclosed-absence NOT_APPLICABLE row is EMITTED: a"
            " 0.2.2-declared pass over a missing spans_earnings without the"
            " counted audit row is a silent pass — exactly what the"
            " declaration refuses to be"
        ),
    ),
    dict(
        id="M332-022-fill-door-decision-close-bypass",
        owner="test_dual_calendar_early_close_decision_fills_at_the_verified_close",
        file="src/tree_options/guards/fills.py",
        anchor="if self.decision_closes is not None:",
        replacement="if False:",
        selectors=[f"{U}/test_backtest_options.py"],
        invariant=(
            "022-C the fill door's DECISION-side comparison consumes the"
            " frozen verified decision closes on the dual-calendar lane:"
            " bypassing the seam re-prices the decision instant against the"
            " execution calendar's nominal 16:00 and rejects the"
            " correctly-stamped 13:00 early-close order"
            " (DECISION_INSTANT_NOT_CLOSE)"
        ),
    ),
    # ---- G4 sealed-event gate machinery (m4/g4-sealed-machinery-20260829) ----------
    dict(
        id="M333-g4-verdict-and-to-or",
        owner="test_a_discipline_violation_in_a_stamped_payload_fails_criterion_and_verdict",
        file="src/tree_options/seal/g4_gate.py",
        anchor='verdict = "PASS" if all(o.verdict == "PASS" for o in outcomes) else "FAIL"',
        replacement='verdict = "PASS" if any(o.verdict == "PASS" for o in outcomes) else "FAIL"',
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 the sealed gate's verdict is FAIL when ANY pre-declared"
            " criterion fails: an all->any short-circuit passes a"
            " discipline-violating payload because some other criterion"
            " passed — the verdict the plan records verbatim would be a lie"
        ),
    ),
    dict(
        id="M334-g4-class-map-counts-no-bar",
        owner="test_the_strict_lane2_class_map_never_counts_no_bar",
        file="src/tree_options/seal/g4_gate.py",
        anchor=(
            "    lane2_counted = (\n"
            '        int(lane2_classes.get("zero_volume_bar_refusals", 0))\n'
            '        + int(lane2_classes.get("massive_derivation_error_refusals", 0))\n'
            '        + int(lane2_classes.get("master_row_refusals", 0))\n'
            "        + flow_fails\n"
            "    )"
        ),
        replacement=(
            "    lane2_counted = (\n"
            '        int(lane2_classes.get("zero_volume_bar_refusals", 0))\n'
            '        + int(lane2_classes.get("massive_derivation_error_refusals", 0))\n'
            '        + int(lane2_classes.get("master_row_refusals", 0))\n'
            "        + flow_fails\n"
            '        + int(lane2_classes.get("no_bar_not_evaluable_disclosed", 0))\n'
            "    )"
        ),
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 criterion 4's STRICT per-lane class map: no_bar NOT_EVALUABLE"
            " rows are an AVAILABILITY DISCLOSURE (~32% of rows by"
            " construction), never pooled into the floor — counting them"
            " inflates a degenerate lane to the pre-declared 50 and proves"
            " nothing (the exact degenerate pass the criterion exists to"
            " refuse)"
        ),
    ),
    dict(
        id="M335-g4-participation-cap-dropped",
        owner="test_over_participation_fails_the_fill_discipline_criterion",
        file="src/tree_options/seal/g4_gate.py",
        anchor=("        if quantity > bar_volumes[(trial, contract, session)]\n    ]"),
        replacement=("        if False\n    ]"),
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 criterion 3's participation clause: cumulative participation"
            " per (trial, contract, bar session) never exceeds the bar's"
            " observed volume (owner ruling 2026-09-02) — dropping the check"
            " certifies a stamped fill sequence that traded more contracts"
            " than the session actually did"
        ),
    ),
    dict(
        id="M380-g4-participation-key-collapsed-to-pooled",
        owner="test_cross_arm_fills_do_not_pool_participation",
        file="src/tree_options/seal/g4_gate.py",
        anchor=("            pair = (key, contract_id, str(bar_session))"),
        replacement=('            pair = ("lane2|pooled", contract_id, str(bar_session))'),
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 criterion 3's participation ledger is PER TRIAL (owner ruling"
            " 2026-09-02, after the event-4 verdict): arms A and B are"
            " independent counterfactual books — a CONSTANT trial component"
            " pools them while preserving the key's arity (Codex remediation-4"
            " P1: the bare 2-tuple collapsed key crashed the reduction's"
            " unpacking, a crash-kill proving nothing), and the pooled ledger"
            " re-fails event-4's shape behaviorally (arm A 3 + arm B 3 > an"
            " observed 4 each book individually respected)"
        ),
    ),
    # ---- theory wave-0 tooling (owner rulings 2026-09-02, P1 package) -------
    dict(
        id="M381-fee-model-stamp-voided",
        owner="test_the_trial_payload_discloses_the_fee_model",
        file="src/tree_options/trials/options_run.py",
        anchor=(
            '    payload["fee_model"] = {\n'
            '        "model": "PerContractFeeModel",\n'
            '        "fee_per_contract": str(PerContractFeeModel.DEFAULT_FEE_PER_CONTRACT),\n'
            '        "minimum_per_order": str(PerContractFeeModel.DEFAULT_MINIMUM_PER_ORDER),\n'
            "    }"
        ),
        replacement=('    payload["fee_model"] = {}'),
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "D6 (theory wave-0): the stamped payload DISCLOSES the fee model"
            " its fills were priced under — the exact PerContractFeeModel"
            " cost constants as string Decimals; an empty stamp certifies"
            " artifacts whose cost basis must be re-derived from source"
        ),
    ),
    dict(
        id="M382-momentum-lookahead-accepted",
        owner="test_momentum_uses_only_pit_visible_closes",
        file="scripts/run_lane2_wave.py",
        anchor=("                if bar.available_at <= close_at\n            )"),
        replacement=("                if bar.session <= session\n            )"),
        selectors=[f"{U}/test_run_lane2_wave.py"],
        invariant=(
            "T-MOM (Agenda C re-denomination): mom_20 reads only closes whose"
            " stamped available_at (the T+1 publication wall) is <= the"
            " decision instant — comparing the SESSION instead leaks the"
            " decision Friday's own close into the score (INV-02 lookahead"
            " in the feature)"
        ),
    ),
    dict(
        id="M383-wave-ledger-drift-refusal-dropped",
        owner="test_the_ledger_roundtrips_and_drift_refuses",
        file="scripts/run_lane2_wave.py",
        anchor=('    if row["params_key"] != key:'),
        replacement=("    if False:"),
        selectors=[f"{U}/test_run_lane2_wave.py"],
        invariant=(
            "wave-0 pre-registration: an execution whose params differ from"
            " the COMMITTED registration row refuses before anything runs —"
            " dropping the comparison lets a pre-registered hypothesis run"
            " under post-hoc parameters"
        ),
    ),
    dict(
        id="M384-wave-sequencing-guard-dropped",
        owner="test_the_sequencing_guard_requires_a_wellformed_calibration",
        file="scripts/run_lane2_wave.py",
        anchor=(
            "    if not isinstance(calibration, dict) or not isinstance(\n"
            '        calibration.get("prior_stride4_cohort_ic_sd"), (int, float)\n'
            "    ):"
        ),
        replacement=("    if False:"),
        selectors=[f"{U}/test_run_lane2_wave.py"],
        invariant=(
            "Agenda A sequencing: a non-null config refuses while the D8"
            " calibration block is absent or malformed — the T-NULL x3 seeds"
            " must run first and their realized stride4_cohort_ic_sd become"
            " the tripwire prior (a truthy-but-malformed block is not"
            " calibration; the synthetic priors are FORBIDDEN on the real"
            " lane)"
        ),
    ),
    dict(
        id="M385-wave-registration-binding-dropped",
        owner="test_the_registration_binding_refuses_rewrites_and_swapped_inputs",
        file="scripts/run_lane2_wave.py",
        anchor=("    if recorded != actual:"),
        replacement=("    if False:"),
        selectors=[f"{U}/test_run_lane2_wave.py"],
        invariant=(
            "wave-0 (Codex P1-3): the TRACKED pre-registration's content"
            " hash must equal the hash the execution state recorded at"
            " registration time — dropping the comparison lets a"
            " registration rewritten after the facts silently govern"
            " executions it never pre-declared"
        ),
    ),
    dict(
        id="M386-calibrate-trusts-unledgered-artifacts",
        owner="test_calibrate_builds_priors_from_the_executed_null_artifacts",
        file="scripts/run_lane2_wave.py",
        anchor=('        if stamp.get("trial_id") != execution["trial_id"]:'),
        replacement=("        if False:"),
        selectors=[f"{U}/test_run_lane2_wave.py"],
        invariant=(
            "wave-0 D8 (Codex P1-4): calibration reads ONLY the artifacts"
            " the execution state recorded, each STAMP-bound to its"
            " recorded trial_id — dropping the stamp check lets planted"
            " unstamped JSON pass as the null seeds' realized evidence"
        ),
    ),
    dict(
        id="M387-holdout-fold-admits-unpermitted-sealed",
        owner="test_holdout_authority_refuses_grid_hygiene_violations",
        file="src/tree_options/trials/options_run.py",
        anchor=("    if unpermitted_sealed:"),
        replacement=("    if False:"),
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "P4 (owner ruling 2026-09-03): the evaluation fold builder"
            " refuses a decision grid carrying a sealed date OUTSIDE the"
            " permit — dropping the refusal lets an unpermitted sealed"
            " session ride the grid as an unevaluated passenger"
        ),
    ),
    dict(
        id="M388-holdout-fold-fills-missing-permitted",
        owner="test_holdout_authority_refuses_grid_hygiene_violations",
        file="src/tree_options/trials/options_run.py",
        anchor=("    if missing:"),
        replacement=("    if False:"),
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "P4: a permitted date ABSENT from the decision grid refuses —"
            " dropping the check lets the fold claim a permitted test"
            " session the grid never carried"
        ),
    ),
    dict(
        id="M389-holdout-authority-window-unchecked",
        owner="test_holdout_authority_shape_and_binding_refusals",
        file="src/tree_options/trials/options_run.py",
        # (window-A extension, 2026-09-04) RE-ANCHORED from the pre-extension
        # equality check to the ratified-set membership line the extension
        # replaced it with; the mutant still drops the check entirely — the
        # window-A-equality variant lives in M412
        anchor=("    if authority.window_id not in RATIFIED_HOLDOUT_WINDOW_IDS:"),
        replacement=("    if False:"),
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "P4: the authority must name a RATIFIED window id — dropping"
            " the check lets a permit for some other window consume"
            " window A's seal"
        ),
    ),
    dict(
        id="M390-seal-disclosure-lies-under-authority",
        owner="test_holdout_authority_executes_the_single_window_a_fold",
        file="src/tree_options/trials/options_run.py",
        anchor=("        reported_intersections: int | list[str] = actual_test_intersections"),
        replacement=("        reported_intersections: int | list[str] = 0"),
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "P4 disclosure honesty: an authorized artifact must report the"
            " REAL test-window/seal intersections — the refusal-era"
            " hardcoded zero would let an evaluation artifact claim it"
            " never touched the window it evaluated"
        ),
    ),
    dict(
        id="M391-end-buffer-one-session-short",
        owner="test_authorized_exits_fill_at_the_calendars_last_session",
        file="src/tree_options/trials/options_run.py",
        anchor=(
            "        min(last_execution_ordinal + END_BUFFER_SESSIONS, len(calendar_sessions) - 1)"
        ),
        replacement=(
            "        min(last_execution_ordinal + END_BUFFER_SESSIONS, len(calendar_sessions) - 2)"
        ),
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "P4 bounded end-buffer: an exit scheduled at the calendar's"
            " FINAL session must fill — shaving one session off the bound"
            " silently truncates the last exits at the world's edge"
        ),
    ),
    dict(
        id="M392-holdout-authority-not-trial-identity",
        owner="test_holdout_authority_is_trial_identity",
        file="src/tree_options/trials/options_run.py",
        anchor="""        # (P4) an authorized window-A evaluation is TRIAL IDENTITY: the
        # authority block rides the config hash so two artifacts can never
        # differ by authorization status under one config hash
        **(
            {
                "holdout_evaluation": {
                    "window_id": holdout_evaluation.window_id,
                    "world_id": holdout_evaluation.world_id,
                    "protocol_hash": holdout_evaluation.protocol_hash_value,
                    "permitted_test_sessions": [
                        d.isoformat() for d in holdout_evaluation.permitted_test_sessions
                    ],
                    "registration_sha256": holdout_evaluation.registration_sha256,
                    "authority_record_sha256": holdout_evaluation.authority_record_sha256,
                    "declared_head": holdout_evaluation.declared_head,
                }
            }
            if holdout_evaluation is not None
            else {}
        ),""",
        replacement="""        # (P4-mutant) the authority block no longer rides the config hash
        **({} if holdout_evaluation is None else {"holdout_evaluation": {"mutant": True}}),""",
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "P4: authorization status is TRIAL IDENTITY — stripping the"
            " authority block from the config hash lets two evaluations"
            " that differ by approval record share one hash"
        ),
    ),
    dict(
        id="M393-f2-bar-not-strict",
        owner="test_f2_requires_both_arms_strictly_above_the_null_max",
        file="src/tree_options/trials/p4_verdict.py",
        anchor=("    f2_anomaly_persisted = min(mom_returns.values()) > null_max"),
        replacement=("    f2_anomaly_persisted = min(mom_returns.values()) >= null_max"),
        selectors=[f"{U}/test_p4_verdict.py"],
        invariant=(
            "P4 ruling 2 (owner 2026-09-03): F2's bar is STRICTLY above the"
            " null max — a tie against the worst null is not anomaly"
            " persistence"
        ),
    ),
    dict(
        id="M394-f1-bar-loosened",
        owner="test_f1_fails_at_one_of_three_negative",
        file="src/tree_options/trials/p4_verdict.py",
        anchor=("    f1_bleed_persisted = negatives >= 2"),
        replacement=("    f1_bleed_persisted = negatives >= 1"),
        selectors=[f"{U}/test_p4_verdict.py"],
        invariant=(
            "P4 ruling 2: F1 needs >=2 of 3 null seeds negative — one bad"
            " seed inside a positive spread is not bleed persistence"
        ),
    ),
    dict(
        id="M395-label-complete-horizon-off-by-one",
        owner="test_real_world_shape_permits_exactly_eight_dates",
        file="src/tree_options/trials/p4_verdict.py",
        anchor=(
            "        if session.isoformat() in sealed and (last_index - index) >= label_horizon_sessions"
        ),
        replacement=(
            "        if session.isoformat() in sealed and (last_index - index) > label_horizon_sessions"
        ),
        selectors=[f"{U}/test_p4_verdict.py"],
        invariant=(
            "P4 ruling 3: label-complete means >= H grid steps of headroom"
            " (the exit-4 consumption matures exactly at H) — an exclusive"
            " bound quietly drops the deepest complete date from the"
            " evaluation"
        ),
    ),
    dict(
        id="M396-p4-second-consumption-allowed",
        owner="test_the_locked_consume_is_one_act",
        file="scripts/run_p4_holdout.py",
        anchor=(
            "        existing = _read_consumptions()\n"
            "        for record in existing:\n"
            '            if record.get("content_identity") == identity:'
        ),
        replacement=(
            "        existing = _read_consumptions()\n"
            "        for record in existing:\n"
            "            if False:"
        ),
        selectors=[f"{U}/test_run_p4_holdout.py"],
        invariant=(
            "P4 one-shot: a consumption record matching the approval's"
            " content identity refuses — dropping the check re-opens the"
            " consumed window for a second look"
        ),
    ),
    dict(
        id="M397-momentum-include-holdout-ignored",
        owner="test_momentum_rows_score_sealed_sessions_only_under_the_flag",
        file="scripts/run_lane2_wave.py",
        anchor=("        if session.isoformat() in holdout and not include_holdout:"),
        replacement=("        if session.isoformat() in holdout:"),
        selectors=[f"{U}/test_run_p4_holdout.py"],
        invariant=(
            "P4: the momentum scorer's seal skip lifts ONLY under"
            " include_holdout (the authorized evaluation) — ignoring the"
            " flag would make every research caller score sealed sessions"
            " too"
        ),
    ),
    dict(
        id="M398-p4-approve-skips-commitment-check",
        owner="test_approve_requires_a_committed_registration",
        file="scripts/run_p4_holdout.py",
        anchor=("    _require_committed_registration(declared_head)"),
        replacement=("    pass  # (mutant) commitment check skipped"),
        selectors=[f"{U}/test_run_p4_holdout.py"],
        invariant=(
            "P4: the approval binds COMMITTED registration content —"
            " skipping the check lets a dirty working tree be approved as"
            " if it were the committed pre-registration"
        ),
    ),
    dict(
        id="M399-p4-verdict-trusts-unstamped-artifacts",
        owner="test_the_verdict_refuses_fabricated_evidence",
        file="scripts/run_p4_holdout.py",
        anchor=('            if body.get("stamp", {}).get("trial_id") != execution["trial_id"]:'),
        replacement=("            if False:"),
        selectors=[f"{U}/test_run_p4_holdout.py"],
        invariant=(
            "P4: the verdict reads ONLY the EXECUTED artifacts — dropping"
            " the stamp binding lets any same-named JSON pass as the"
            " window-A evidence"
        ),
    ),
    dict(
        id="M400-p4-approval-lookalike-accepted",
        owner="test_approval_lookalikes_refuse_by_name",
        file="scripts/run_p4_holdout.py",
        anchor=('    if record.get("kind") != "P4_HOLDOUT_APPROVAL":'),
        replacement=("    if False:"),
        selectors=[f"{U}/test_run_p4_holdout.py"],
        invariant=(
            "P4 (Codex round 1 P1-1): the approval RECORD is the owner act —"
            " a hand-written lookalike with a foreign kind must refuse, or"
            " any JSON object on that path governs the window"
        ),
    ),
    dict(
        id="M401-p4-verdict-accepts-escaped-paths",
        owner="test_the_verdict_refuses_fabricated_evidence",
        file="scripts/run_p4_holdout.py",
        anchor=(
            "            if not posixpath.normpath(artifact_path).startswith(expected_prefix):"
        ),
        replacement=("            if False:"),
        selectors=[f"{U}/test_run_p4_holdout.py"],
        invariant=(
            "P4 (Codex round 1 P1-3): verdict evidence lives only under the"
            " driver's own per-slot trial directory (normpath-collapsed) —"
            " dropping the check lets a ..-bearing path point anywhere"
        ),
    ),
    dict(
        id="M402-p4-verdict-without-consumption",
        owner="test_the_verdict_requires_a_consumption",
        file="scripts/run_p4_holdout.py",
        anchor=("    if not consumed:"),
        replacement=("    if False:"),
        selectors=[f"{U}/test_run_p4_holdout.py"],
        invariant=(
            "P4 (Codex round 1 P1-3): the verdict follows a CONSUMPTION — a"
            " bare state file cannot certify window-A outcomes nothing"
            " spent"
        ),
    ),
    dict(
        id="M404-p4-tracked-evidence-refusal-dropped",
        owner="test_the_tracked_evidence_file_refuses_a_second_consumption",
        file="scripts/run_p4_holdout.py",
        anchor=("    if EVIDENCE_PATH.is_file():"),
        replacement=("    if False:"),
        selectors=[f"{U}/test_run_p4_holdout.py"],
        invariant=(
            "P4 (Codex round 1 P1-2): the TRACKED evidence record travels"
            " with the repo — its existence refuses the window even from a"
            " second checkout whose local ledger never saw the spend"
        ),
    ),
    # ---- successor packet 2026-09-03: canonical out_root + harness guards ----
    dict(
        id="M405-census-out-root-lexical",
        owner="test_a_symlink_dotted_out_root_commits_the_actual_parents_exit_0",
        file="scripts/build_coverage_census.py",
        anchor=(
            "    # 0. (PR #13's round-16 KNOWN DEBT, repaired — the R18 shape)"
            " bind ONE\n"
            "    # canonical resolved out_root BEFORE any classification,"
            " mkdir,\n"
            "    # emission, or durability walk. A symlink- or `..`-laden"
            " spelling made\n"
            "    # the kernel-resolved EMISSION land on the real chain while"
            " the lexical\n"
            "    # abspath the no-follow walks traversed committed the DECOY"
            " chain — the\n"
            "    # real hierarchy's entries stayed uncommitted at attestation."
            " Every\n"
            "    # downstream consumer now sees the same resolved path, so"
            " there is no\n"
            "    # second spelling left to walk.\n"
            "    args.out_root = Path(os.path.realpath(args.out_root))"
        ),
        replacement=(
            "    # 0. (PR #13's round-16 KNOWN DEBT, repaired — the R18 shape)"
            " bind ONE\n"
            "    # canonical resolved out_root BEFORE any classification,"
            " mkdir,\n"
            "    # emission, or durability walk. A symlink- or `..`-laden"
            " spelling made\n"
            "    # the kernel-resolved EMISSION land on the real chain while"
            " the lexical\n"
            "    # abspath the no-follow walks traversed committed the DECOY"
            " chain — the\n"
            "    # real hierarchy's entries stayed uncommitted at attestation."
            " Every\n"
            "    # downstream consumer now sees the same resolved path, so"
            " there is no\n"
            "    # second spelling left to walk.\n"
            "    args.out_root = Path(os.path.abspath(args.out_root))"
        ),
        selectors=[f"{U}/test_coverage_census.py"],
        invariant=(
            "PR #13 R18: out_root is bound ONCE as the canonical RESOLVED"
            " path — realpath, never the lexical abspath. The lexical form"
            " re-opens the round-16 decoy: the kernel-resolved emission"
            " lands on the real chain while the no-follow walks commit the"
            " decoy chain and the real parent's entry stays uncommitted at"
            " attestation (owner horns: exit-0 AND exit-5, actual-parent"
            " fsync + decoy-never-written)"
        ),
    ),
    dict(
        id="M406-criterion6-nonfull-selection-accepted",
        owner="test_an_iteration_mode_report_is_not_gate_authority",
        file="src/tree_options/seal/g4_gate.py",
        anchor=('        if selection_mode != "full":'),
        replacement=('        if False and selection_mode != "full":'),
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "criterion 6 accepts ONLY a full-registry campaign: an --only or"
            " --changed-since report (or a pre-2026-09-03 shape with no"
            " selection block) is iteration evidence, never gate authority"
            " — a sharded --jobs run is still FULL and stays authority"
        ),
    ),
    dict(
        id="M407-harness-baseline-cache-not-cleared",
        owner="test_a_harness_error_clears_the_baseline_cache",
        file="scripts/mutate.py",
        anchor=(
            "            r = run_mutant(wt, m, baseline_cache=cache)\n"
            '            if r["verdict"] == "HARNESS_ERROR":\n'
            "                cache.clear()"
        ),
        replacement=(
            "            r = run_mutant(wt, m, baseline_cache=cache)\n"
            '            if r["verdict"] == "HARNESS_ERROR":\n'
            "                pass  # cache-clear dropped: a damaged tree keeps"
            " stale baselines"
        ),
        selectors=[f"{U}/test_mutate_harness.py"],
        invariant=(
            "harness: any HARNESS_ERROR verdict clears the shard's baseline"
            " cache — restore is byte-verified per mutant, so a damaged tree"
            " can no longer be assumed pristine and cached baseline passes"
            " stop being statements about this tree"
        ),
    ),
    # ---- window-A extension lane (owner direction 2026-09-04) ----------------
    dict(
        id="M408-ext-scope-not-derived",
        owner="test_the_extension_scope_is_the_unconsumed_remainder",
        file="scripts/run_p4_holdout.py",
        anchor=(
            "    if not _ACTIVE_WINDOW.extends_window_a:\n"
            "        return frozenset(FINAL_HOLDOUT_DATES)"
        ),
        replacement=("    if True:\n        return frozenset(FINAL_HOLDOUT_DATES)"),
        selectors=[f"{U}/test_p4_window_extension.py"],
        invariant=(
            "the extension's date scope is DERIVED from the spent packet —"
            " FINAL_HOLDOUT_DATES minus window A's registered permitted set."
            " Returning the full enumeration for the extension re-opens the"
            " consumed dates to a second look (the one-shot the seal exists"
            " to prevent)"
        ),
    ),
    dict(
        id="M409-ext-base-evidence-unrequired",
        owner="test_the_extension_requires_the_consumed_base",
        file="scripts/run_p4_holdout.py",
        anchor=("    if not WINDOW_A_EVIDENCE_PATH.is_file():"),
        replacement=("    if False and not WINDOW_A_EVIDENCE_PATH.is_file():"),
        selectors=[f"{U}/test_p4_window_extension.py"],
        invariant=(
            "the extension may only exist AFTER the base window was"
            " consumed — the tracked window-A evidence file is the durable"
            " cross-checkout proof of that spend; without it a"
            " never-consumed base window could be silently widened instead"
            " of extended"
        ),
    ),
    dict(
        id="M410-ext-permitted-unscoped",
        owner="test_extension_permitted_filters_out_window_a_dates",
        file="scripts/run_p4_holdout.py",
        anchor=("    scoped = tuple(d for d in permitted if d.isoformat() in scope)"),
        replacement=("    scoped = tuple(permitted)"),
        selectors=[f"{U}/test_p4_window_extension.py"],
        invariant=(
            "the permitted set is the label-complete sessions INTERSECTED"
            " with the active window's scope — on a fully-grown world the"
            " extension still yields ONLY the five unconsumed dates;"
            " window A's spent dates can never ride a second evaluation"
        ),
    ),
    dict(
        id="M411-approval-spent-date-accepted",
        owner="test_extension_approval_validates_against_the_ext_scope",
        file="scripts/run_p4_holdout.py",
        anchor=("    if spent:"),
        replacement=("    if False and spent:"),
        selectors=[f"{U}/test_p4_window_extension.py"],
        invariant=(
            "an approval whose permitted set smuggles a window-A-consumed"
            " date refuses by name — spent dates are the one-shot; a"
            " lookalike approval that swaps in a wider window is exactly"
            " the forgery the record validation exists to catch"
        ),
    ),
    dict(
        id="M412-authority-ext-window-refused",
        owner="test_the_authority_shape_accepts_the_ratified_extension_id",
        file="src/tree_options/trials/options_run.py",
        anchor=("    if authority.window_id not in RATIFIED_HOLDOUT_WINDOW_IDS:"),
        replacement=("    if authority.window_id != FINAL_HOLDOUT_WINDOW_ID:"),
        selectors=[f"{U}/test_p4_window_extension.py"],
        invariant=(
            "the authority shape accepts every RATIFIED holdout window id"
            " (window A and its extension) and refuses everything else —"
            " reverting to window-A equality alone makes the ratified"
            " extension unevaluable and untested ids pass the same check"
        ),
    ),
    dict(
        id="M413-launcher-protocol-refrozen",
        owner="test_the_launcher_requires_the_current_protocol_version",
        file="scripts/launch_bars_era.py",
        anchor=('REQUIRED_BARS_PROTOCOL_VERSION = "0.2.2"'),
        replacement=('REQUIRED_BARS_PROTOCOL_VERSION = "0.2.1"'),
        selectors=[f"{U}/test_p4_window_extension.py", f"{U}/test_launch_bars_era.py"],
        invariant=(
            "the continuation capture runs under the LIVE protocol 0.2.2"
            " (owner-ratified in the flip); re-freezing the gate at 0.2.1"
            " closes the era launcher against the only protocol the"
            " continuation approval can bind"
        ),
    ),
    dict(
        id="M414-ext-run-index-reuse",
        owner="test_the_extension_rebinds_every_surface",
        file="scripts/run_p4_holdout.py",
        anchor=("    _NULL_RUN_BASE = window.null_run_base"),
        replacement=("    _NULL_RUN_BASE = 15"),
        selectors=[f"{U}/test_p4_window_extension.py"],
        invariant=(
            "the extension's arm-A trials continue the per-arm run-index"
            " namespace (a-r20..r24, b-r4) — reusing window A's base"
            " (15..19, b-3) mints colliding trial identifiers across the"
            " two packets' per-arm namespaces"
        ),
    ),
    dict(
        id="M415-ext-trials-prefix-static",
        owner="test_the_trials_prefix_follows_the_binding",
        file="scripts/run_p4_holdout.py",
        anchor=('    return TRIALS_DIR.relative_to(REPO_ROOT).as_posix() + "/"'),
        replacement=('    return "artifacts/theory/p4/trials/"'),
        selectors=[f"{U}/test_p4_window_extension.py"],
        invariant=(
            "the verdict's artifact-path prefix is derived from the ACTIVE"
            " window's trials directory — a static window-A prefix accepts"
            " window-A artifacts as extension verdict evidence (and refuses"
            " every genuine extension artifact)"
        ),
    ),
    dict(
        id="M416-work-manifest-rewrite",
        owner="test_the_work_manifest_wrapper_is_write_once",
        file="scripts/build_bars_work_manifest.py",
        # (Codex round 1 F5) RE-ANCHORED onto the atomic create: the
        # exists()-then-write pair was a TOCTOU and its early refusal is
        # gone; the write-once discipline now lives in O_EXCL — dropping
        # it makes the open TRUNCATE an existing (possibly
        # approval-bound) manifest
        anchor=(
            "        fd = os.open(args.out, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644)"
        ),
        replacement=(
            "        fd = os.open(args.out, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o644)"
        ),
        selectors=[f"{U}/test_p4_window_extension.py"],
        invariant=(
            "the work-manifest wrapper is write-once — the output is"
            " created atomically with O_CREAT|O_EXCL|O_NOFOLLOW; a rebuilt"
            " manifest is a NEW manifest and an approval may already bind"
            " the existing file's raw sha256, so a silent replace (or a"
            " truncating open) strands that approval against bytes"
            " nothing holds"
        ),
    ),
    dict(
        id="M418-ext-partial-tranche-accepted",
        owner="test_extension_refuses_a_partial_tranche",
        file="scripts/run_p4_holdout.py",
        anchor=("    if immature:"),
        replacement=("    if False and immature:"),
        selectors=[f"{U}/test_p4_window_extension.py"],
        invariant=(
            "Codex round 1 F1: the extension consumes ALL of its derived"
            " scope or NONE of it — the window is one-shot, so a partial"
            " registration would irreversibly strand the still-immature"
            " dates; a later look at them needs a NEWLY ratified window"
        ),
    ),
    dict(
        id="M419-ext-base-registration-uncommitted-accepted",
        owner="test_the_base_registration_must_be_committed_clean",
        file="scripts/run_p4_holdout.py",
        anchor=("    if committed is None or committed != raw:"),
        replacement=("    if False:"),
        selectors=[f"{U}/test_p4_window_extension.py"],
        invariant=(
            "Codex round 1 F2: the extension scope derives from the"
            " CANONICAL spent packet — the base registration must read"
            " committed-clean at HEAD (on-disk bytes == committed bytes),"
            " so an offline working-tree rewrite can never widen or"
            " narrow the derived scope"
        ),
    ),
    dict(
        id="M420-ext-root-alias-unchecked",
        owner="test_a_preplanted_root_alias_refuses_the_binding",
        file="scripts/run_p4_holdout.py",
        anchor=("            if component.is_symlink():"),
        replacement=("            if False:"),
        selectors=[f"{U}/test_p4_window_extension.py"],
        invariant=(
            "Codex round 1 F3: the bound window's writable surfaces are"
            " real directories-in-waiting — a preplanted symlinked"
            " component routes extension state/registry/trials writes"
            " into the spent packet's tree (or a volatile root); the"
            " bind-time component walk refuses it before any write"
        ),
    ),
    dict(
        id="M417-committed-registration-path-static",
        owner="test_committed_registration_check_uses_the_active_window_path",
        file="scripts/run_p4_holdout.py",
        anchor=("    registration_rel = REGISTRATION_PATH.relative_to(REPO_ROOT).as_posix()"),
        replacement=('    registration_rel = "docs/theory/p4-window-a-registration.json"'),
        selectors=[f"{U}/test_p4_window_extension.py"],
        invariant=(
            "the committed-registration check names the ACTIVE window's"
            " tracked path — a static window-A path lets an extension"
            " approval be satisfied by the window-A registration being"
            " committed (and would refuse every genuine extension"
            " registration)"
        ),
    ),
    # ---- window-A extension CONTINUATION (owner ruling 2026-09-04: the
    # two-cycle capture — enablement packet M421-M430) ----------------------
    dict(
        id="M421-window-empty-refusal-dropped",
        owner="test_as_of_min_that_filters_everything_refuses_naming_the_filter",
        file="src/tree_options/data/bars_manifest.py",
        anchor="        if not windowed:",
        replacement="        if False:",
        selectors=[f"{U}/test_bars_manifest.py"],
        invariant=(
            "(round-2 re-pin, 2026-09-04: the original post-pick boundary"
            " anchor moved to M433 when the filter became pre-pick) a window"
            " that selects no masters must refuse NAMING THE FILTER — voiding"
            " the check lets the empty window fall through to selection and"
            " surface as the wrong (profile-empty) refusal, hiding the"
            " configuration defect the message exists to name"
        ),
    ),
    dict(
        id="M422-window-assignment-dropped",
        owner="test_the_window_re_selects_a_contract_first_chosen_before_the_window",
        file="src/tree_options/data/bars_manifest.py",
        anchor="        captures = windowed",
        replacement="        pass",
        selectors=[f"{U}/test_bars_manifest.py"],
        invariant=(
            "(round-2 re-pin, 2026-09-04: the original post-pick filter-drop"
            " anchor moved to M432 when the filter became pre-pick) computing"
            " the windowed master list without APPLYING it re-pins the whole"
            " grid — the continuation manifest would authorize dates the"
            " standing era already captured and mis-attribute window"
            " re-selections to history"
        ),
    ),
    dict(
        id="M423-verify-regeneration-unfiltered",
        owner="test_as_of_min_manifest_verifies_by_regeneration",
        file="src/tree_options/data/bars_manifest.py",
        anchor=("        as_of_min=manifest.as_of_min,"),
        replacement=("        as_of_min=None,"),
        selectors=[f"{U}/test_bars_manifest.py"],
        invariant=(
            "verification regenerates through the manifest's OWN filter —"
            " an unfiltered rebuild diverges from every continuation"
            " manifest, so threading None would refuse all of them (and"
            " never notice a manifest whose entries predate its declared"
            " filter)"
        ),
    ),
    dict(
        id="M424-build-canonical-iso-dropped",
        owner="test_non_canonical_as_of_min_refuses",
        file="src/tree_options/data/bars_manifest.py",
        anchor=('            _require_canonical_iso_date(as_of_min, field="as_of_min")'),
        replacement=("            pass  # mutant: canonical-ISO check dropped"),
        selectors=[f"{U}/test_bars_manifest.py"],
        invariant=(
            "as_of comparison is lexicographic over ISO text — a"
            " non-canonical filter date (2025-3-19) compares the wrong"
            " side of every canonical date, silently filtering everything"
            " or nothing; the build refuses it in the manifest error family"
        ),
    ),
    dict(
        id="M425-model-canonical-iso-dropped",
        owner="test_a_non_canonical_as_of_min_refuses_at_parse",
        file="src/tree_options/data/bars_manifest.py",
        anchor=('        _require_canonical_iso_date(value, field="as_of_min")'),
        replacement=("        pass  # mutant: model-level canonical-ISO check dropped"),
        selectors=[f"{U}/test_bars_manifest.py"],
        invariant=(
            "the MODEL refuses a non-canonical as_of_min at parse time —"
            " the build-side check alone leaves hand-written manifest JSON"
            " unguarded (an approval must never bind a manifest whose"
            " filter text compares wrong)"
        ),
    ),
    dict(
        id="M426-duplicate-approval-guard-dropped",
        owner="test_second_approval_of_the_same_tuple_refused_under_lock",
        file="src/tree_options/data/bars_manifest.py",
        anchor=("        guard=_refuse_duplicate_approval(protocol_hash, work_manifest_sha256),"),
        replacement=("        guard=None,"),
        selectors=[f"{U}/test_bars_manifest.py"],
        invariant=(
            "one approval per (protocol, work manifest) tuple: a duplicate"
            " APPROVAL adds no authority the first does not carry, and a"
            " rewritten reason must not ride what looks like a fresh grant"
            " — refused under the ledger lock, race-safe"
        ),
    ),
    dict(
        id="M427-duplicate-guard-protocol-only",
        owner="test_approval_of_the_same_protocol_over_a_new_work_manifest_appends",
        file="src/tree_options/data/bars_manifest.py",
        anchor=(
            "                and record.protocol_hash == protocol_hash\n"
            "                and record.work_manifest_sha256 == work_manifest_sha256"
        ),
        replacement=("                and record.protocol_hash == protocol_hash"),
        selectors=[f"{U}/test_bars_manifest.py"],
        invariant=(
            "the duplicate-approval guard keys on the TUPLE, never the"
            " protocol alone — keying on the protocol would refuse the"
            " legal two-cycle continuation shape (a NEW work manifest"
            " approved under the same live protocol)"
        ),
    ),
    dict(
        id="M428-approval-cli-binds-content-hash",
        owner="test_appends_exactly_one_record_binding_the_verified_bytes",
        file="scripts/append_bars_launch_approval.py",
        anchor=("    work_file_sha = hashlib.sha256(raw).hexdigest()"),
        replacement=("    work_file_sha = manifest.content_sha256"),
        selectors=[f"{U}/test_append_bars_launch_approval.py"],
        invariant=(
            "the approval record binds the work manifest's RAW FILE sha256"
            " — exactly what the launcher's authority join compares; binding"
            " the model content hash instead strands every approval against"
            " a digest nothing joins on"
        ),
    ),
    dict(
        id="M429-approval-cli-version-unpinned",
        owner="test_a_stale_protocol_version_refuses",
        file="scripts/append_bars_launch_approval.py",
        anchor=("    if protocol.meta.protocol_version != REQUIRED_BARS_PROTOCOL_VERSION:"),
        replacement=("    if False:"),
        selectors=[f"{U}/test_append_bars_launch_approval.py"],
        invariant=(
            "the continuation approval binds the LIVE protocol version —"
            " an unpinned CLI would mint a record under a stale protocol"
            " yaml that the re-opened launcher gate can never open"
        ),
    ),
    dict(
        id="M430-wrapper-verify-regeneration-skipped",
        owner="test_the_wrapper_verify_mode_is_read_only",
        file="scripts/build_bars_work_manifest.py",
        anchor=(
            "    verify_bars_work_manifest(\n"
            "        manifest,\n"
            "        profile=profile,\n"
            "        capture_manifest_sha256=capture_manifest_sha,\n"
            "        capture_dir=args.capture_dir,\n"
            "    )"
        ),
        replacement=(
            "    _ = (profile, capture_manifest_sha, args.capture_dir)  # mutant: verify skipped"
        ),
        selectors=[f"{U}/test_p4_window_extension.py"],
        invariant=(
            "the wrapper's --verify REGENERATES the file through the same"
            " library path the launcher will use — a parse-only verify"
            " would bless a tampered or drifted manifest the preflight"
            " must refuse"
        ),
    ),
    # ---- (Codex round-1 NO-GO fixes, 2026-09-04) continuation round-2
    # enablement mutants M431-M440 ------------------------------------------
    dict(
        id="M431-legacy-hash-null-included",
        owner="test_a_pre_field_legacy_manifest_still_hashes_as_it_did",
        file="src/tree_options/data/bars_manifest.py",
        anchor='    exclude = {"as_of_min"} if core.as_of_min is None else None',
        replacement="    exclude = None",
        selectors=[f"{U}/test_bars_manifest.py"],
        invariant=(
            "an unset as_of_min must never enter the content-hash preimage —"
            " serializing it as null re-hashes every standing era manifest"
            " (the real artifacts/bars/work-manifest.json stopped verifying"
            " under exactly this bug)"
        ),
    ),
    dict(
        id="M432-window-filter-dropped",
        owner="test_the_window_re_selects_a_contract_first_chosen_before_the_window",
        file="src/tree_options/data/bars_manifest.py",
        anchor="        windowed = [c for c in captures if c.as_of >= window_floor]",
        replacement="        windowed = list(captures)",
        selectors=[f"{U}/test_bars_manifest.py"],
        invariant=(
            "the continuation window must dedupe WITHIN itself: windowing"
            " after a global selection drops every contract first chosen"
            " before the window yet re-listed inside it, so the approved"
            " manifest would not cover what the launch actually fetches"
        ),
    ),
    dict(
        id="M433-window-floor-exclusive",
        owner="test_as_of_min_pins_only_the_continuation_work",
        file="src/tree_options/data/bars_manifest.py",
        anchor="        windowed = [c for c in captures if c.as_of >= window_floor]",
        replacement="        windowed = [c for c in captures if c.as_of > window_floor]",
        selectors=[f"{U}/test_bars_manifest.py"],
        invariant=(
            "the window bound is INCLUSIVE at the declared Friday — the"
            " continuation manifest must carry the window's own master, not"
            " silently refuse or start one Friday late"
        ),
    ),
    dict(
        id="M434-spot-merge-skipped",
        owner="test_spot_merge_existing_unions_history_and_is_idempotent",
        file="scripts/capture_massive_structural.py",
        anchor="                    spot = merge_spot_proxy(spot_proxy_path, spot)",
        replacement="                    pass",
        selectors=[f"{U}/test_capture_massive_structural.py"],
        invariant=(
            "--spot-merge-existing must actually merge: skipping it lets the"
            " continuation overwrite the v1 spot history with only its own"
            " dates, destroying the label surface every scoped decision"
            " stands on"
        ),
    ),
    dict(
        id="M435-spot-merge-conflict-accepted",
        owner="test_spot_merge_refuses_a_conflicting_close",
        file="scripts/capture_massive_structural.py",
        anchor="            if prior is not None and prior != close:",
        replacement="            if False:",
        selectors=[f"{U}/test_capture_massive_structural.py"],
        invariant=(
            "a different close for an already-pinned session is a vendor"
            " revision of history — accepting it would silently rewrite the"
            " pinned past instead of demanding an owner ruling"
        ),
    ),
    dict(
        id="M436-manifest-mode-bypass-dropped",
        owner="test_bars_from_manifest_fetches_exactly_the_approved_work",
        file="scripts/capture_massive_structural.py",
        anchor="        if manifest_picks is not None:",
        replacement="        if False:",
        selectors=[f"{U}/test_capture_massive_structural.py"],
        invariant=(
            "--bars-from-manifest must REPLACE the selection: falling back"
            " to a fresh in-run pick re-decides the approved work, so the"
            " wire traffic would no longer be what the owner approved"
        ),
    ),
    dict(
        id="M437-standing-series-overwritten",
        owner="test_bars_from_manifest_fetches_exactly_the_approved_work",
        file="scripts/capture_massive_structural.py",
        anchor="                if ticker not in set(standing)",
        replacement="                if True",
        selectors=[f"{U}/test_capture_massive_structural.py"],
        invariant=(
            "a manifest series already on disk is left standing and never"
            " re-fetched — overwriting it would replace the era's captured"
            " bytes with a later-start series under the same approval"
        ),
    ),
    dict(
        id="M438-inventory-check-dropped",
        owner="test_bars_from_manifest_reports_an_inventory_mismatch",
        file="scripts/capture_massive_structural.py",
        anchor=(
            "        if missing:\n"
            "            print(\n"
            '                f"INVENTORY MISMATCH: {len(missing)} work-manifest entries have no"'
        ),
        replacement=(
            "        if False:\n"
            "            print(\n"
            '                f"INVENTORY MISMATCH: {len(missing)} work-manifest entries have no"'
        ),
        selectors=[f"{U}/test_capture_massive_structural.py"],
        invariant=(
            "the manifest-driven run ends with an inventory equality claim —"
            " voiding the check reports success while approved entries have"
            " no series on disk"
        ),
    ),
    dict(
        id="M439-cli-regeneration-skipped",
        owner="test_bars_from_manifest_refuses_a_tampered_manifest",
        file="scripts/capture_massive_structural.py",
        anchor=(
            "            verify_bars_work_manifest(\n"
            "                work_manifest,\n"
            "                profile=load_selection_profile(args.selection_profile),\n"
            "                capture_manifest_sha256=sha256_hex(capture_manifest_raw),\n"
            "                capture_dir=args.out_dir,\n"
            "            )"
        ),
        replacement=(
            "            _ = (work_manifest, args.selection_profile,\n"
            "                 capture_manifest_raw, args.out_dir)  # mutant: verify skipped"
        ),
        selectors=[f"{U}/test_capture_massive_structural.py"],
        invariant=(
            "the bridge's --bars-from-manifest verifies by FULL regeneration"
            " against the capture dir — without it a re-hashed tampered"
            " manifest (self-hash consistent, entries lying) sails through"
            " to the wire"
        ),
    ),
    dict(
        id="M440-builder-writes-null-filter",
        owner="test_the_wrapper_from_as_of_builds_the_continuation_manifest",
        file="scripts/build_bars_work_manifest.py",
        anchor='    exclude = None if manifest.as_of_min is not None else {"as_of_min"}',
        replacement="    exclude = None",
        selectors=[f"{U}/test_p4_window_extension.py"],
        invariant=(
            "a legacy-shape build's FILE omits the unset filter — writing"
            ' "as_of_min": null breaks byte-shape parity with the standing'
            " era's manifests for no gain"
        ),
    ),
    # ---- spotv2 capture lane (owner ruling 2026-08-29 "Capture it") ----------
    dict(
        id="M336-spotv2-close-through-float",
        owner="test_vendor_close_tokens_round_trip_byte_exact",
        file="scripts/capture_spot_proxy_v2.py",
        anchor=("    if isinstance(raw_c, Decimal):\n        exact_close = raw_c"),
        replacement=(
            "    if isinstance(raw_c, Decimal):\n        exact_close = Decimal(str(float(raw_c)))"
        ),
        selectors=[f"{U}/test_capture_spot_proxy_v2.py"],
        invariant=(
            "spotv2 the vendor's close TOKEN is the provenance: routing the"
            " exact Decimal through float rewrites every price token longer"
            " than a float's shortest repr (a 21-digit close silently loses"
            " its tail) while short tokens survive, so only a byte-exact"
            " round trip through the real loader catches the laundering"
        ),
    ),
    dict(
        id="M337-spotv2-vendor-gap-silenced",
        owner="test_a_vendor_gap_fails_the_run_naming_the_session",
        file="scripts/capture_spot_proxy_v2.py",
        anchor="        if missing:",
        replacement="        if False:",
        selectors=[f"{U}/test_capture_spot_proxy_v2.py"],
        invariant=(
            "spotv2 an era session the vendor response cannot answer is a"
            " NAMED fatal gap and NOTHING is written: voiding the check"
            " writes a silently short file that still claims the declared"
            " window — the exact partial capture this lane exists to refuse"
        ),
    ),
    # ---- G4 bar-boundary price quantization (m4/g4-price-boundary-20260831) --------
    dict(
        id="M338-g4-bar-boundary-quantize-dropped",
        owner="test_a_three_decimal_wire_close_quantizes_the_flat_bar_at_the_boundary",
        file="src/tree_options/trials/g4_event.py",
        anchor="                quantized = close.quantize(PRICE_TICK, rounding=ROUND_HALF_EVEN)",
        replacement="                quantized = close",
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 the flat dataset bar quantizes every sub-cent wire close to"
            " the cent tick AT the BarRecord boundary with an explicit"
            " ROUND_HALF_EVEN: dropping the quantize feeds a real-wire 3dp"
            ' close (ADBE "417.125" — the exact class that crashed the'
            " 2026-08-31 sealed event before any trial ran) straight into"
            " the shared 2dp Price type and kills the one-shot run at"
            " world-build time"
        ),
    ),
    dict(
        id="M339-g4-quantization-custody-counter-zeroed",
        owner="test_the_spot_close_quantization_block_stamps_the_exact_census_values",
        file="src/tree_options/trials/g4_event.py",
        anchor="                spot_close_quantized_rows += 1",
        replacement="                spot_close_quantized_rows += 0",
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 the boundary quantization is CUSTODY, not silence: every row"
            " the tick rewrites is counted and stamped in the census payload —"
            " zeroing the counter rewrites a quantizing world as an exact one"
            " while its bars still carry the moved closes"
        ),
    ),
    dict(
        id="M340-g4-sub-cent-refusal-dropped",
        owner="test_a_sub_cent_positive_close_refuses_naming_the_row",
        file="src/tree_options/trials/g4_event.py",
        anchor="            if quantized <= 0:",
        replacement="            if False:",
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 a positive sub-cent close quantizes to 0.00, which Price"
            " (gt=0) can never carry: the build must REFUSE naming the"
            " underlying, session, and original token — dropping the guard"
            " surfaces the anonymous pydantic gt=0 violation instead of the"
            " named refusal (or floors the row to a zero price)"
        ),
    ),
    dict(
        id="M341-g4-quantization-max-delta-comparator-flipped",
        owner="test_multi_row_custody_tracks_the_largest_value_movement",
        file="src/tree_options/trials/g4_event.py",
        anchor="                    or delta > spot_close_max_quantization_delta",
        replacement="                    or delta < spot_close_max_quantization_delta",
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 the custody max_delta is the LARGEST value movement among"
            " rewritten rows: flipping the comparator keeps a smaller late"
            " delta (0.004 over 0.005) and understates the boundary's worst"
            " case — a single-row world can never catch it"
        ),
    ),
    dict(
        id="M342-g4-exponent-gate-dropped",
        owner="test_only_two_decimal_closes_carry_through_exactly_with_no_custody_noise",
        file="src/tree_options/trials/g4_event.py",
        anchor="            if exponent < -2:",
        replacement="            if True:",
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 only a close whose wire EXPONENT exceeds the cent tick is"
            " rewritten: quantizing unconditionally re-serializes every"
            " on-grid row (an exponent-0 '6E+2' becomes '600.00') while"
            " numeric Decimal equality hides the drift and custody reports"
            " zero — the quiet path must stay representation-exact"
        ),
    ),
    dict(
        id="M343-g4-criterion6-stale-report-binding-dropped",
        owner="test_a_stale_mutation_report_fails_criterion_six_against_the_live_registry",
        file="src/tree_options/seal/g4_gate.py",
        anchor="            if missing:",
        replacement="            if False:",
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 criterion 6 binds the mutation report to the LIVE registry at"
            " this head: an N/N report that omits registry mutants (the stale"
            " report new registry entries would leave behind without a"
            " re-run) must FAIL — dropping the binding certifies a campaign"
            " that never covered this head's guards"
        ),
    ),
    dict(
        id="M344-g4-criterion6-registry-absence-silenced",
        owner="test_a_stale_mutation_report_fails_criterion_six_against_the_live_registry",
        file="src/tree_options/seal/g4_gate.py",
        anchor="        if mutation_registry_ids is None or mutation_registry_digest is None:",
        replacement="        if False:",
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 criterion 6 with NO live registry supplied must fail with the"
            " NAMED absence failure — dropping the check degrades the refusal"
            " to an anonymous every-id-foreign failure (or worse), losing the"
            " never-silently-skipped contract the criterion's text declares"
        ),
    ),
    dict(
        id="M345-g4-runner-preflight-dropped",
        owner="test_a_malformed_report_makes_the_runner_preflight_refuse",
        file="src/tree_options/seal/runner.py",
        anchor="        preflight_gate_auxiliaries(paths=production_gate_paths(repo), repo_root=repo)",
        replacement="        pass",
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 the production runner PREFLIGHTS the gate's auxiliary inputs"
            " BEFORE the one-shot event runs: dropping the preflight lets an"
            " unparseable report raise only after the event created the"
            " sealed registry/artifacts — consumed authority with no verdict,"
            " the exact 2026-08-31 failure mode"
        ),
    ),
    dict(
        id="M346-g4-criterion6-head-binding-dropped",
        owner="test_a_stale_mutation_report_fails_criterion_six_against_the_live_registry",
        file="src/tree_options/seal/g4_gate.py",
        anchor="            if not isinstance(stamped_head, str) or stamped_head != head:",
        replacement="            if False:",
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 criterion 6 binds the report to the SEALED head: the registry"
            " can be identical across commits while the guarded code moved,"
            " so a registry-bound report from another head is still stale —"
            " dropping the binding certifies a campaign that ran against"
            " different code"
        ),
    ),
    dict(
        id="M347-g4-execute-preflight-dropped",
        owner="test_execute_preflights_the_runner_before_the_consumption_is_durable",
        file="scripts/g4_seal.py",
        anchor="    cast(Any, runner).preflight()",
        replacement="    pass",
        selectors=[f"{U}/test_g4_seal.py"],
        invariant=(
            "G4 execute_sealed_run PREFLIGHTS the runner before the"
            " CONSUMPTION append: the record is durable the moment it is"
            " written, and a runner that would raise on an unevaluable"
            " auxiliary input must refuse BEFORE the append — otherwise the"
            " refusal itself consumes the one-shot and leaves UNKNOWN"
            " authority with no verdict"
        ),
    ),
    dict(
        id="M348-g4-restoration-flag-coerced",
        owner="test_a_stale_mutation_report_fails_criterion_six_against_the_live_registry",
        file="src/tree_options/seal/g4_gate.py",
        anchor=('        restoration = mutation_report.get("restoration_suite_passed") is True'),
        replacement=(
            '        restoration = bool(mutation_report.get("restoration_suite_passed", False))'
        ),
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 criterion 6's restoration flag is a STRICT boolean: the"
            ' STRING "false" is truthy under bool() and a report carrying it'
            " certified a campaign whose restoration suite never passed —"
            " coercion is a lie, not a pass"
        ),
    ),
    dict(
        id="M349-g4-registry-derive-unsafe",
        owner="test_post_preflight_auxiliary_changes_fail_criteria_never_raise",
        file="src/tree_options/seal/g4_gate.py",
        anchor="        except GatePreflightError:",
        replacement="        except AssertionError:",
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 the evaluation-side registry derive must be exception-safe:"
            " a registry that became unloadable AFTER the preflight (and so"
            " after the CONSUMPTION) is a post-spend shape change — a"
            " criterion-6 FAIL verdict, never a propagated"
            " GatePreflightError with no verdict"
        ),
    ),
    dict(
        id="M350-g4-replay-hash-unsafe",
        owner="test_post_preflight_auxiliary_changes_fail_criteria_never_raise",
        file="src/tree_options/seal/g4_gate.py",
        anchor="            except OSError:\n                replay_hashes = None",
        replacement="            except AssertionError:\n                replay_hashes = None",
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 a PRESENT but partial replay directory FAILs criterion 5 as a"
            " verdict: payload_hashes reads eagerly, so an absent replay"
            " payload would raise FileNotFoundError AFTER consumption — the"
            " honest absent-replay failure is the criterion's, never an"
            " exception's"
        ),
    ),
    dict(
        id="M351-g4-era-count-bool-clause-dropped",
        owner="test_an_era_census_with_non_integer_counts_refuses_at_preflight",
        file="src/tree_options/seal/g4_gate.py",
        anchor="        if isinstance(value, bool) or not isinstance(value, int):",
        replacement="        if not isinstance(value, int):",
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 an era-census count must be a true int, BOOLS EXCLUDED: JSON"
            " true parses to Python True, which subclasses int (True == 1)"
            " and would certify a count that was never stamped as a number —"
            " floats and strings still refuse via the int clause, so only"
            " the bool case guards this"
        ),
    ),
    dict(
        id="M352-g4-replay-alias-check-dropped",
        owner="test_an_aliased_replay_cannot_certify_determinism_by_self_comparison",
        file="src/tree_options/seal/g4_gate.py",
        anchor=(
            "        replay_aliased = paths.replay_artifacts.resolve() == run.artifacts_dir.resolve() or any(\n"
            "            _symlinked(path) or _shares_inode(path, stamped_paths[name])\n"
            "            for name, path in replay_map.items()\n"
            "        )"
        ),
        replacement="        replay_aliased = False  # mutant: alias check dropped",
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 criterion 5 certifies a CLEAN-CLONE replay: a replay whose"
            " payloads symlink onto the run's own artifacts (or whose dir IS"
            " the artifacts dir) compares byte-identical by construction —"
            " dropping the alias check lets self-comparison certify"
            " determinism, which is no certification at all"
        ),
    ),
    dict(
        id="M353-g4-deep-json-recursion-uncaught",
        owner="test_deeply_nested_auxiliary_json_never_raises_post_consumption",
        file="src/tree_options/seal/g4_gate.py",
        anchor='    except (OSError, RecursionError, ValueError) as exc:\n        raise GatePreflightError(\n            f"the era census {paths.era_census} cannot be parsed ({exc!r}) —"',
        replacement='    except (OSError, ValueError) as exc:\n        raise GatePreflightError(\n            f"the era census {paths.era_census} cannot be parsed ({exc!r}) —"',
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 a deeply nested auxiliary JSON raises RecursionError from"
            " json.loads — a RuntimeError the ValueError handlers cannot"
            " contain; the preflight must refuse it (and the evaluation"
            " convert it to a verdict), never let a raw escape follow the"
            " consumption"
        ),
    ),
    dict(
        id="M354-g4-eval-deep-json-recursion-uncaught",
        owner="test_post_preflight_auxiliary_changes_fail_criteria_never_raise",
        file="src/tree_options/seal/g4_gate.py",
        anchor="        except (OSError, RecursionError, ValueError, KeyError, TypeError) as exc:",
        replacement="        except (OSError, ValueError, KeyError, TypeError) as exc:",
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 the EVALUATION-side census parse must contain RecursionError:"
            " a deeply nested census swapped in after the preflight (and so"
            " after the CONSUMPTION) FAILs criterion 1 as a verdict — the"
            " preflight catch alone cannot protect the post-spend path"
        ),
    ),
    dict(
        id="M355-g4-inode-check-vanish-uncaught",
        owner="test_a_replay_payload_vanishing_mid_check_never_raises",
        file="src/tree_options/seal/g4_gate.py",
        anchor=(
            "            except OSError:\n"
            "                return False\n"
            "            return (replay_stat.st_dev, replay_stat.st_ino) == ("
        ),
        replacement=(
            "            except AssertionError:\n"
            "                return False\n"
            "            return (replay_stat.st_dev, replay_stat.st_ino) == ("
        ),
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 the alias check's stat can hit a payload that VANISHES"
            " mid-check (exists-then-stat is a race): the OSError is absence"
            " — criterion 5's own missing-payload failure — never a raw"
            " FileNotFoundError after the CONSUMPTION"
        ),
    ),
    dict(
        id="M356-g4-rearm-budget-voided",
        owner="test_reconciliation_re_arms_consumed_content_for_exactly_one_successor",
        file="scripts/g4_seal.py",
        anchor="    if content_consumptions > reconciliations:",
        replacement="    if False:  # mutant: the re-arm budget is never enforced",
        selectors=[f"{U}/test_g4_seal.py"],
        invariant=(
            "G4 each owner RECONCILIATION permits exactly ONE further"
            " consumption of the content: a third checkout without a second"
            " owner act refuses"
        ),
    ),
    dict(
        id="M357-g4-reconciliation-count-dropped",
        owner="test_reconciliation_re_arms_consumed_content_for_exactly_one_successor",
        file="scripts/g4_seal.py",
        anchor=(
            "            if reconciliation_content_id == content_id:\n"
            "                reconciliations += 1\n"
            "                if reconciliations > content_consumptions:\n"
            "                    raise LedgerCorruptError(\n"
            '                        f"RECONCILIATION record {record.record_sha256[:12]}… is"\n'
            '                        " credited AHEAD of any consumption of this content"\n'
            '                        f" (prefix holds {reconciliations} reconciliation(s)"\n'
            '                        f" against {content_consumptions} consumption(s)) —"\n'
            '                        " authority is never granted ahead of the spend it"\n'
            '                        " names, not even in a hash-valid hand-chained ledger"\n'
            "                    )"
        ),
        replacement=(
            "            pass  # mutant: the reconciliation budget is never"
            " credited and the prefix order rule never raises"
        ),
        selectors=[f"{U}/test_g4_seal.py"],
        invariant=(
            "G4 the successor consumption is permitted only while"
            " reconciliations(content) covers it: an uncounted"
            " reconciliation re-arms nothing"
        ),
    ),
    dict(
        id="M358-g4-exact-run-arm-counted-not-refused",
        owner="test_the_exact_consumed_checkout_stays_refused_even_after_reconciliation",
        file="scripts/g4_seal.py",
        anchor=(
            "        if record_run_id == run_id:\n"
            "            raise SecondExecutionRefusedError(\n"
            '                run_id, "a CONSUMPTION record already matches this exact sealed run"\n'
            "            )"
        ),
        replacement=(
            "        content_consumptions += 1  # mutant: the exact-run arm counted, never refused"
        ),
        selectors=[f"{U}/test_g4_seal.py"],
        invariant=(
            "G4 the sealed-RUN arm is absolute: the crashed checkout is"
            " never re-runnable regardless of any reconciliation budget"
            " (the remediation is a different head by construction)"
        ),
    ),
    dict(
        id="M359-g4-reconciliation-minted-ahead-of-spend",
        owner="test_reconciliation_requires_an_existing_matching_consumption",
        file="src/tree_options/seal/ledger.py",
        anchor="    if not consumed_here:",
        replacement="    if False:  # mutant: reconciliation minted ahead of any consumption",
        selectors=[f"{U}/test_g4_seal.py"],
        invariant=(
            "G4 a reconciliation re-arms an EXISTING consumed-without-verdict"
            " spend only: minting one ahead of a consumption pre-authorizes"
            " a re-run, which is a second approval channel, never authority"
        ),
    ),
    dict(
        id="M360-g4-run-key-validation-voided",
        owner="test_a_run_key_must_be_a_sealed_run_id_shaped_token",
        file="src/tree_options/seal/g4_gate.py",
        anchor='    if not re.fullmatch(r"[0-9a-f]{64}", run_key):',
        replacement="    if False:  # mutant: any run key steers the workspace path",
        selectors=[f"{U}/test_g4_run_scoped_paths.py"],
        invariant=(
            "G4 the run-scoped workspace directory is named by a sealed run"
            " id (64 lowercase hex): traversal-shaped or partial keys can"
            " never steer authority outputs outside the run root"
        ),
    ),
    dict(
        id="M361-g4-run-scoping-falls-back-to-legacy",
        owner="test_a_run_key_scopes_the_sealed_workspace_under_g4_sealed_runs",
        file="src/tree_options/seal/g4_gate.py",
        anchor='    run_root = repo_root / "artifacts" / "g4-sealed-runs" / run_key',
        replacement='    run_root = repo_root / "artifacts"  # mutant: the run key does not scope',
        selectors=[f"{U}/test_g4_run_scoped_paths.py"],
        invariant=(
            "G4 one workspace per sealed run: without the run-key scoping a"
            " successor checkout collides with the crashed event's occupied"
            " legacy outputs and the one-shot refuses"
        ),
    ),
    dict(
        id="M362-g4-runner-drops-run-key",
        owner="test_the_production_runner_delegates_to_the_machinery",
        file="src/tree_options/seal/runner.py",
        anchor="        gate_paths = production_gate_paths(repo, run_key=run_id)",
        replacement="        gate_paths = production_gate_paths(repo)  # mutant: legacy fixed paths",
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 the production runner derives its outputs from THIS run's"
            " sealed_run_id — the run-scoped workspace the"
            " successor-enablement lane exists to give it"
        ),
    ),
    dict(
        id="M363-g4-forged-reconciliation-ids-trusted",
        owner="test_forged_reconciliation_stored_ids_refused_as_corrupt",
        file="scripts/g4_seal.py",
        anchor=(
            "            if (\n"
            "                record.content_identity != reconciliation_content_id\n"
            "                or record.sealed_run_id != sealed_run_id(record.identity)\n"
            "            ):"
        ),
        replacement="            if False:  # mutant: forged reconciliation stored ids trusted",
        selectors=[f"{U}/test_g4_seal.py"],
        invariant=(
            "G4 the re-arm budget is computed from RECOMPUTED ids, never a"
            " RECONCILIATION record's stored ones (the M229 rule, extended"
            " to the authority-bearing reconciliation kind)"
        ),
    ),
    dict(
        id="M364-g4-verdict-guard-runscoped-voided",
        owner="test_the_guarded_reconciliation_refuses_a_run_scoped_verdict",
        file="scripts/g4_seal.py",
        anchor="    if run_scoped_summary.is_file():",
        replacement="    if False:  # mutant: the run-scoped verdict guard never refuses",
        selectors=[f"{U}/test_g4_seal.py"],
        invariant=(
            "G4 the driver-path reconciliation is verdict-aware at the"
            " run-scoped workspace: a sealed-gate-summary.json for the"
            " consumed checkout means the verdict EXISTS and re-arming"
            " verdicted content is not reconciliation"
        ),
    ),
    dict(
        id="M365-g4-verdict-guard-legacy-voided",
        owner="test_the_guarded_reconciliation_refuses_a_legacy_verdict",
        file="scripts/g4_seal.py",
        anchor="    if legacy_summary.is_file():",
        replacement="    if False:  # mutant: the legacy verdict guard never refuses",
        selectors=[f"{U}/test_g4_seal.py"],
        invariant=(
            "G4 the verdict guard also covers the LEGACY artifacts layout"
            " (the one the 2026-08-31 crashed event ran under): a"
            " sealed-gate-summary.json there is an existing verdict for the"
            " consumed content"
        ),
    ),
    dict(
        id="M366-g4-outstanding-spend-rule-voided",
        owner="test_a_second_reconciliation_requires_a_new_consumption",
        file="src/tree_options/seal/ledger.py",
        anchor="    if consumptions <= reconciliations:",
        replacement="    if False:  # mutant: stockpiled reconciliations accepted",
        selectors=[f"{U}/test_g4_seal.py"],
        invariant=(
            "G4 a reconciliation re-arms an OUTSTANDING consumed-without-"
            " verdict spend: minting another while the crash is already"
            " covered stockpiles authority ahead of any second failure"
        ),
    ),
    dict(
        id="M367-g4-prefix-order-rule-voided",
        owner="test_a_reconciliation_credited_ahead_of_its_consumption_is_corrupt",
        file="scripts/g4_seal.py",
        anchor=(
            "                if reconciliations > content_consumptions:\n"
            "                    raise LedgerCorruptError("
        ),
        replacement="                if False:\n                    raise LedgerCorruptError(",
        selectors=[f"{U}/test_g4_seal.py"],
        invariant=(
            "G4 causal order at every prefix: a hash-valid reconciliation"
            " credited AHEAD of any consumption of its content is corruption"
            " the budget arithmetic may never honor"
        ),
    ),
    dict(
        id="M368-g4-hosting-root-binding-voided",
        owner="test_the_verdict_guard_is_bound_to_the_hosting_root",
        file="scripts/g4_seal.py",
        anchor="    if not hosted_here:",
        replacement="    if False:  # mutant: any root attests verdict absence",
        selectors=[f"{U}/test_g4_seal.py"],
        invariant=(
            "G4 the verdict-absence check binds to the HOSTING root (run-"
            " scoped workspace or legacy residue): a negative filesystem"
            " check against a root that hosted nothing is not evidence"
        ),
    ),
    dict(
        id="M369-g4-symlinked-workspace-accepted",
        owner="test_a_symlinked_run_workspace_component_refuses",
        file="src/tree_options/trials/g4_event.py",
        anchor=(
            "            if stat.S_ISLNK(mode):\n"
            '                raise RuntimeError(f"refusing a symlinked sealed workspace'
            ' component: {component}")'
        ),
        replacement=(
            "            if False:  # mutant: a symlinked workspace component is"
            " accepted\n"
            "                pass"
        ),
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 a directory symlink in the sealed workspace's ancestor chain"
            " redirects registry/artifacts/scratch/replay outside the"
            " checkout while lexical checks stay green — the run refuses it"
            " before a single byte is created"
        ),
    ),
    dict(
        id="M370-g4-custody-identity-voided",
        owner="test_criterion1_is_the_custody_identity",
        file="src/tree_options/seal/g4_gate.py",
        anchor=("    if verified_series + master_row_refusals != era_contracts:"),
        replacement=(
            "    if verified_series != era_contracts:  # mutant: the raw"
            " equality, refused custody invisible"
        ),
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 criterion 1's target is the CUSTODY IDENTITY (verified +"
            " counted master-row refusals == stamped, the 2026-09-01 owner"
            " ruling): honest refusals are custody, and only a row accounted"
            " for by NEITHER side is silent loss"
        ),
    ),
    dict(
        id="M371-g4-lane1-floor-voided",
        owner="test_the_real_lane1_floor_is_zero",
        file="src/tree_options/seal/g4_gate.py",
        anchor="    if lane1_counted < lane1_floor:",
        replacement="    if False:  # mutant: the lane-1 floor never enforces",
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 the lane-1 floor is a PARAMETER (0 real / explicit fixture):"
            " the explicit fixture floor must still FAIL a clean lane 1 —"
            " an unenforced floor proves nothing"
        ),
    ),
    dict(
        id="M372-g4-census-manifest-binding-voided",
        owner="test_criterion1_binds_the_run_census_to_the_verified_packet",
        file="src/tree_options/seal/g4_gate.py",
        anchor="    if stamped_manifest_hash != lane2_manifest_content_hash:",
        replacement="    if False:  # mutant: a foreign census certifies anything",
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 the run's lane-2 census must name THE manifest the held"
            " packet verified (Codex remediation-2 P1-1): a foreign or"
            " stale census satisfies no arithmetic — its counts certify"
            " nothing"
        ),
    ),
    dict(
        id="M373-g4-cli-lane1-floor-default-forced",
        owner="test_the_gate_cli_requires_yes_and_then_records_the_verdict",
        file="scripts/run_m4_sealed_gate.py",
        anchor="        default=REJECTION_LANE1_FLOOR,",
        replacement="        default=REJECTION_FLOOR,  # mutant: fixture floor forced on real data",
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 the CLI (the documented real-data entry) threads the"
            " ruling's lane-1 floor 0 by default (Codex remediation-2"
            " P1-2): the floor policy must not depend on the entrypoint"
        ),
    ),
    dict(
        id="M374-g4-daily-spot-consult-voided",
        owner="test_the_daily_spot_source_unblocks_the_non_friday_session",
        file="src/tree_options/data/massive_overlay.py",
        anchor=(
            "        daily = self._spot_v2.get(sid)\n"
            "        if daily and session in daily:\n"
            "            return daily[session]"
        ),
        replacement=(
            "        daily = self._spot_v2.get(sid)\n"
            "        if False and daily and session in daily:\n"
            "            return daily[session]  # mutant: the daily source is ignored"
        ),
        selectors=[f"{U}/test_massive_overlay.py"],
        invariant=(
            "G4 the derivation's underlying spot consults the declared DAILY"
            " source first (owner ruling 2026-09-02): event-3 failed"
            " criterion 2 precisely because the Friday-only v1 proxy left"
            " every T+1-visible non-Friday cell spot-less (no_in_band_strike"
            " 312/312, zero candidates)"
        ),
    ),
    dict(
        id="M375-g4-daily-spot-validation-dropped",
        owner="test_an_injected_non_finite_daily_close_refuses_at_construction",
        file="src/tree_options/data/massive_overlay.py",
        anchor="                v2_rows[v2_session] = _validated_spot_token(v2_where, v2_session, v2_close)",
        replacement=(
            "                v2_rows[v2_session] = v2_close  # mutant: the copy"
            " loop launders what a file cannot"
        ),
        selectors=[f"{U}/test_massive_overlay.py"],
        invariant=(
            "G4 the v2 copy loop validates every close through the shared"
            " spot-token discipline (the R5-P2/R4-P2 class): an injected"
            " Infinity close must refuse at construction, never flow into"
            " intrinsic and the election policy"
        ),
    ),
    dict(
        id="M376-g4-packet-sidecar-binding-voided",
        owner="test_the_sidecar_binds_into_the_packet",
        file="src/tree_options/seal/verified_inputs.py",
        anchor=(
            '        "spot_proxy_v2_sha256": spot_v2_sha,\n'
            "    }\n"
            "    core = VerifiedSealedInputs.model_construct(\n"
            "        schema_version=VERIFIED_INPUTS_SCHEMA_VERSION,\n"
            "        code_sha=code_sha,\n"
            "        protocol_hash=protocol_sha,\n"
            "        lane1_manifest=lane1,\n"
            "        lane2_manifest=lane2,\n"
            "        calendar_decision_artifact_sha256=calendar_sha,\n"
            "        criteria_artifact_sha256=criteria_sha,\n"
            "        criteria_source_document_sha256=criteria_source_sha,\n"
            "        runner_version=RUNNER_VERSION,\n"
            "        runner_implementation_sha256=registered.implementation_sha256,\n"
            "        runner_implementation_qualname=registered.implementation_qualname,\n"
            "        runner_config_digest=registered.config_digest,\n"
            "        spot_proxy_v2_sha256=spot_v2_sha,\n"
            '        packet_content_sha256="",'
        ),
        replacement=(
            '        "spot_proxy_v2_sha256": None,  # mutant: the sidecar rides nothing\n'
            "    }\n"
            "    core = VerifiedSealedInputs.model_construct(\n"
            "        schema_version=VERIFIED_INPUTS_SCHEMA_VERSION,\n"
            "        code_sha=code_sha,\n"
            "        protocol_hash=protocol_sha,\n"
            "        lane1_manifest=lane1,\n"
            "        lane2_manifest=lane2,\n"
            "        calendar_decision_artifact_sha256=calendar_sha,\n"
            "        criteria_artifact_sha256=criteria_sha,\n"
            "        criteria_source_document_sha256=criteria_source_sha,\n"
            "        runner_version=RUNNER_VERSION,\n"
            "        runner_implementation_sha256=registered.implementation_sha256,\n"
            "        runner_implementation_qualname=registered.implementation_qualname,\n"
            "        runner_config_digest=registered.config_digest,\n"
            "        spot_proxy_v2_sha256=None,  # mutant: both sites, the packet stays self-consistent\n"
            '        packet_content_sha256="",'
        ),
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 the v2 sidecar's hash is bound into the packet's"
            " self-binding (owner ruling 2026-09-02): it feeds the"
            " derivation's daily spot, so the packet must pin its bytes —"
            " an unbound sidecar is swappable research content"
        ),
    ),
    dict(
        id="M377-g4-runner-drops-held-sidecar",
        owner="test_the_run_consumes_the_held_sidecar_bytes_not_the_path",
        file="src/tree_options/trials/g4_event.py",
        anchor=(
            "    spot_v2_path: Path | None = None\n"
            "    if held.spot_proxy_v2_bytes is not None:\n"
            '        spot_v2_path = scratch / "spot-proxy-v2.json"\n'
            "        spot_v2_path.write_bytes(held.spot_proxy_v2_bytes)"
        ),
        replacement=(
            "    spot_v2_path: Path | None = None\n"
            "    if False and held.spot_proxy_v2_bytes is not None:\n"
            '        spot_v2_path = scratch / "spot-proxy-v2.json"\n'
            "        spot_v2_path.write_bytes(held.spot_proxy_v2_bytes)  # mutant: held sidecar dropped"
        ),
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 the run materializes and consumes the HELD sidecar bytes in"
            " its own scratch (M262's discipline extended): dropping them"
            " silently reverts the run to the v1-only spot semantics the"
            " event-3 verdict already refused, and the census's"
            " spot_v2_declared + derivation_spot_source disclose it"
        ),
    ),
    dict(
        id="M378-g4-criterion2-starvation-note-voided",
        owner="test_criterion2_names_the_starvation_counter",
        file="src/tree_options/seal/g4_gate.py",
        anchor="        if starved > 0 and arm_failures:",
        replacement=(
            "        if False and starved > 0 and arm_failures:  # mutant: the"
            " counter is never named"
        ),
        selectors=[f"{U}/test_g4_event_machinery.py"],
        invariant=(
            "G4 criterion 2's failure text NAMES the stamped"
            " no_in_band_strike counter (owner ruling 2026-09-02): a future"
            " FAIL verdict must self-point at the starvation cause instead"
            " of reading as a silent nothing-happened"
        ),
    ),
    dict(
        id="M379-non-monotone-ladder-refusal-dropped",
        owner="test_build_candidates_counts_and_skips_a_non_monotone_ladder",
        file="src/tree_options/options/strategy.py",
        anchor=(
            "        except NonMonotoneLadderError:\n"
            "            # (remediation-3) counted per-name refusal, never a run abort:\n"
            "            # on the real lane this is a data property of one underlying's\n"
            "            # derived ladder (META/2025-12-19 surfaced it live), and a fatal\n"
            "            # raise here would spend the sealed authority and record no\n"
            "            # verdict — the exact 2026-08-31 crash class\n"
            "            if audit is not None:\n"
            "                audit.non_monotone_ladder += 1\n"
            "            continue"
        ),
        replacement=(
            "        except NonMonotoneLadderError:\n"
            "            raise  # mutant: one name's data condition aborts the run"
        ),
        selectors=[f"{U}/test_options_strategy.py"],
        invariant=(
            "STRATEGY one underlying's non-monotone |delta| ladder is a"
            " counted per-name refusal (the M165 discipline), never a"
            " whole-run abort: a fatal raise here re-opens the"
            " consumed-authority-no-verdict class on the real lane"
            " (META/2025-12-19, live probe 2026-09-02)"
        ),
    ),
]


def registry_digest() -> str:
    """sha256 over the canonical MUTANTS list — the PRODUCER side of
    criterion 6's registry binding: stamped into every report this runner
    writes and recomputed by the G4 gate's ``live_mutation_registry`` at
    evaluation time. A report from a different registry revision (or a
    hand-forged one) does not carry the matching value. Any change to the
    digest formula here MUST be mirrored there — the machinery test pins
    the two together."""
    return hashlib.sha256(
        json.dumps(MUTANTS, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# Only tracked source/config/docs belong in the disposable mutation checkout.
# Generated outputs can contain deliberately adversarial names from custody
# tests (including dangling symlinks and rename/recreate probes), and authority
# artifacts must never be propagated into another checkout.
DISPOSABLE_COPY_IGNORE = (
    ".venv",
    "__pycache__",
    ".git",
    "*.pyc",
    ".pytest_cache",
    "artifacts",
    "dist",
)

FAILING = ("FAILED",)


def _run(worktree: Path, args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", *args], cwd=worktree, capture_output=True, text=True, timeout=timeout
    )


def run_mutant(worktree: Path, mutant: dict, baseline_cache: dict | None = None) -> dict:
    path = worktree / mutant["file"]
    original = path.read_text()
    pre_hash = hashlib.sha256(original.encode()).hexdigest()
    result = {
        "id": mutant["id"],
        "invariant": mutant["invariant"],
        "selectors": mutant["selectors"],
        "source_sha256": pre_hash,
        "verdict": None,
        "detail": "",
    }
    count = original.count(mutant["anchor"])
    if count != 1:
        result["verdict"] = "MUTATION_DRIFT"
        result["detail"] = f"anchor occurs {count}x (expected 1)"
        return result

    # Baseline: the owning selectors must pass BEFORE mutation (one retry —
    # a transient toolchain hiccup must not be misread as a harness error).
    # The baseline is CACHEABLE per selector set (success packet 2026-09-03):
    # restore is byte-verified per mutant (a mismatch is a HARNESS_ERROR),
    # so within a shard the tree is provably pristine whenever a later
    # baseline would run — the cached pass still describes this tree. The
    # shard loop CLEARS the cache on any HARNESS_ERROR verdict; only
    # successful baselines are ever stored.
    cache_key = tuple(mutant["selectors"])
    base = baseline_cache.get(cache_key) if baseline_cache is not None else None
    if base is None:
        import time

        for _attempt in range(3):
            try:
                base = _run(
                    worktree,
                    ["pytest", *mutant["selectors"], "-q", "-p", "no:cacheprovider"],
                    600,
                )
            except subprocess.TimeoutExpired:
                result["verdict"], result["detail"] = "HARNESS_ERROR", "baseline timeout"
                return result
            if base.returncode == 0:
                break
            time.sleep(2)
        if base is None or base.returncode != 0:
            tail = (base.stdout if base else "").strip().splitlines()[-1:] or ["<no output>"]
            result["verdict"] = "HARNESS_ERROR"
            result["detail"] = f"baseline selectors fail: {tail[0][:100]}"
            return result
        if baseline_cache is not None:
            baseline_cache[cache_key] = base

    path.write_text(original.replace(mutant["anchor"], mutant["replacement"]))
    try:
        compile_proc = subprocess.run(
            [sys.executable, "-m", "py_compile", str(path)], capture_output=True, text=True
        )
        if compile_proc.returncode != 0:
            result["verdict"] = "INVALID_MUTANT"
            result["detail"] = "mutant does not compile"
            return result
        try:
            proc = _run(
                worktree,
                ["pytest", *mutant["selectors"], "-q", "--tb=no", "-p", "no:cacheprovider"],
                600,
            )
        except subprocess.TimeoutExpired:
            result["verdict"], result["detail"] = "TIMEOUT", "owning selectors exceeded 600s"
            return result
        out = proc.stdout + proc.stderr
        failed_lines = [ln for ln in out.splitlines() if ln.startswith(FAILING)]
        owner_failures = [ln for ln in failed_lines if mutant["owner"] in ln]
        if owner_failures:
            result["verdict"] = "KILLED"
            result["detail"] = owner_failures[0][:120]
        elif failed_lines:
            # Other tests failed but the OWNING test passed: never a kill —
            # the mutant is provoking collateral damage, not a caught defect.
            result["verdict"] = "INVALID_MUTANT"
            result["detail"] = f"non-owner failures only: {failed_lines[0][:90]}"
        elif proc.returncode == 0:
            result["verdict"] = "SURVIVED"
            result["detail"] = "owning selectors passed under the mutant"
        else:
            # nonzero exit without FAILED lines: collection error / crash —
            # never credited as a behavioral kill.
            result["verdict"] = "INVALID_MUTANT"
            result["detail"] = next((ln for ln in out.splitlines() if ln.strip()), "")[:120]
    finally:
        path.write_text(original)
        # Purge bytecode caches for the mutated module's package: a
        # length-identical mutant restored within the same mtime second
        # would otherwise keep serving the MUTATED .pyc to later runs.
        pycache = path.parent / "__pycache__"
        if pycache.is_dir():
            shutil.rmtree(pycache, ignore_errors=True)
        restored = path.read_text() == original
    if not restored:
        result["verdict"], result["detail"] = "HARNESS_ERROR", "restore not byte-exact"
    return result


def _split_contiguous(items: list, parts: int) -> list[list]:
    """Split into at most `parts` CONTIGUOUS chunks (registry order
    preserved — concatenating the chunks in order reproduces the input;
    file locality stays inside one shard because the registry groups by
    target file)."""
    if parts <= 1 or len(items) <= 1:
        return [list(items)]
    size, rem = divmod(len(items), parts)
    chunks: list[list] = []
    start = 0
    for i in range(parts):
        end = start + size + (1 if i < rem else 0)
        if start < end:
            chunks.append(list(items[start:end]))
        start = end
    return chunks or [[]]


def _prepare_worktree(scratch_parent: Path) -> tuple[Path, Path]:
    """One disposable copy + synthetic git baseline + frozen env. Returns
    (worktree_root, repo_dir). A failure anywhere inside (copy, synthetic
    git, uv sync) removes the PARTIAL tree it created — the root is
    allocated before anything can fail, so nothing leaks (successor Codex
    round, P2-5)."""
    worktree = Path(tempfile.mkdtemp(prefix=f"{REPO.name}-mutate-", dir=scratch_parent))
    try:
        shutil.copytree(
            REPO,
            worktree / "repo",
            ignore=shutil.ignore_patterns(*DISPOSABLE_COPY_IGNORE),
        )
        wt = worktree / "repo"
        # The copy excludes .git by design, but WS-F stamping (build_stamp)
        # fail-closes without a usable repository: git rev-parse HEAD plus a
        # clean tree. The three runs of the M3 campaign all failed the
        # restoration suite solely on test_run_options_trial_end_to_end for
        # exactly this reason (retained worktree: no .git at all). A
        # synthetic baseline commit provides both without inheriting state
        # from the source checkout; mutant apply/restore is file-byte based,
        # so .git is inert to the loop. Committed BEFORE uv sync so the
        # .venv it creates stays ignored, and .gitignore (copied with the
        # tree) keeps artifacts/ and dist/ out of the baseline as well.
        for _cmd in (
            ["git", "-c", "init.defaultBranch=main", "init", "-q"],
            ["git", "add", "-A"],
            [
                "git",
                "-c",
                "user.email=harness@localhost",
                "-c",
                "user.name=mutation harness",
                "commit",
                "-q",
                "-m",
                "mutation harness baseline",
            ],
        ):
            subprocess.run(_cmd, cwd=wt, capture_output=True, check=True)
        subprocess.run(
            ["uv", "sync", "--frozen"], cwd=wt, capture_output=True, timeout=600, check=True
        )
    except BaseException:
        shutil.rmtree(worktree, ignore_errors=True)
        raise
    return worktree, wt


def _run_shard(mutants: list[dict], scratch_parent: Path) -> tuple[list[dict], Path, Path]:
    """Run one slice of the registry in this thread's OWN disposable copy
    (per-mutant subprocesses are the work, so threads parallelize without
    sharing anything but the read-only registry). The baseline cache lives
    per shard and is CLEARED on any HARNESS_ERROR verdict: a damaged tree
    can no longer be assumed pristine, so cached baseline passes stop
    being statements about this tree. Returns (results, worktree_root,
    repo_dir); the caller owns cleanup, this function only cleans up its
    own copy on failure."""
    worktree, wt = _prepare_worktree(scratch_parent)
    try:
        cache: dict = {}
        results = []
        for m in mutants:
            r = run_mutant(wt, m, baseline_cache=cache)
            if r["verdict"] == "HARNESS_ERROR":
                cache.clear()
            results.append(r)
        return results, worktree, wt
    except BaseException:
        shutil.rmtree(worktree, ignore_errors=True)
        raise


def _changed_files(ref: str) -> set[str]:
    proc = subprocess.run(
        ["git", "diff", "--name-only", ref], cwd=REPO, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise SystemExit(f"--changed-since: bad ref {ref!r}: {proc.stderr.strip()[:160]}")
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _select_changed_since(mutants: list[dict], changed: set[str]) -> list[dict]:
    """The NON-AUTHORITY iteration selection: a mutant is relevant when its
    TARGET file or any of its owning SELECTOR files changed vs the ref."""
    return [m for m in mutants if m["file"] in changed or any(s in changed for s in m["selectors"])]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None)
    parser.add_argument(
        "--head",
        default=None,
        help=(
            "the git head the campaign ran at (the orchestrator passes"
            " `git rev-parse HEAD`); stamped into the report so criterion 6"
            " can bind it to the sealed head — a report from another head"
            " is stale and FAILs"
        ),
    )
    parser.add_argument(
        "--only",
        nargs="+",
        default=None,
        metavar="ID",
        help=(
            "run only the named mutant ids (debt-lane evidence for new"
            " mutants and re-pins; the full registry run stays the"
            " m0_gate's authority — 323 through PR #21 + M336/M337 spotv2"
            " + M338-M355 price-boundary + M356-M363 successor-enablement"
            " + M364-M369 remediation + M370-M373 remediation-2 +"
            " M374-M379 remediation-3 + M380 remediation-4 + M381-M386"
            " theory wave-0 + M387-M404 P4 window-A + M405-M407 successor"
            " + M408-M420 window-A extension + M421-M430 extension"
            " continuation + M431-M440 Codex round-2 fixes here)"
        ),
    )
    parser.add_argument(
        "--changed-since",
        default=None,
        metavar="REF",
        help=(
            "NON-AUTHORITY iteration mode: run only mutants whose target"
            " file or owning selector files changed vs REF (git diff"
            " --name-only REF). Iteration evidence only — criterion 6"
            " refuses any report whose selection is not the full campaign"
        ),
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        metavar="N",
        help=(
            "run the campaign across N worker shards, each in its own"
            " disposable copy (threads; the work is per-mutant"
            " subprocesses). Verdicts, report schema, and the restoration"
            " proof are identical to the serial run; only wall-clock"
            " changes (plus a per-shard copy+uv-sync setup cost)"
        ),
    )
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be >= 1")
    if args.only is not None and args.changed_since is not None:
        parser.error("--only and --changed-since are mutually exclusive")
    selected = list(MUTANTS)
    selection_mode = "full"
    selection_ref = None
    if args.only is not None:
        wanted = set(args.only)
        known = {m["id"] for m in MUTANTS}
        unknown = sorted(wanted - known)
        if unknown:
            parser.error(f"unknown mutant ids: {unknown}")
        selected = [m for m in MUTANTS if m["id"] in wanted]
        selection_mode = "only"
    elif args.changed_since is not None:
        changed = _changed_files(args.changed_since)
        selected = _select_changed_since(MUTANTS, changed)
        selection_mode = "changed-since"
        selection_ref = args.changed_since

    # The disposable copy must honor the same host rule the seal/bars
    # authority ledgers enforce mechanically: nothing repo-authoritative
    # may resolve under /tmp (wiped on reboot). The A4/A5 ledger tests
    # place their scratch at repo-relative artifacts/ paths precisely to
    # stay off /tmp; a /tmp-based copy makes those paths resolve under
    # /tmp, the (correct) LedgerRootRefusedError then fails every owning
    # test at baseline, and the gate reports HARNESS_ERROR for those
    # mutants plus a failed restoration suite (first full-gate attempt
    # 2026-08-23: 10x HARNESS_ERROR, all M208-M212/M214-M218). Scratch
    # therefore lives BESIDE the repo — never under /tmp, never inside
    # the tree being copied (copytree into a subdir of its own source
    # would recurse). TREE_OPTIONS_MUTATE_ROOT overrides for operators.
    scratch_parent = Path(os.environ.get("TREE_OPTIONS_MUTATE_ROOT") or REPO.parent)
    actual_jobs = max(1, min(args.jobs, len(selected) or 1))
    chunks = _split_contiguous(selected, actual_jobs)
    keep_worktrees = False
    shards: list[tuple[list[dict], Path, Path]] = []
    try:
        if actual_jobs == 1:
            shards = [_run_shard(chunks[0], scratch_parent)]
        else:
            from concurrent.futures import ThreadPoolExecutor

            pool = ThreadPoolExecutor(max_workers=actual_jobs)
            try:
                pending = [pool.submit(_run_shard, chunk, scratch_parent) for chunk in chunks]
                # futures are collected in SUBMIT order, so the merged
                # results are registry order by construction (chunks are
                # contiguous slices); results are appended INCREMENTALLY so
                # a shard exception still leaves the already-completed
                # siblings in `shards` for the finally-cleanup — the
                # all-or-nothing comprehension leaked them (successor Codex
                # round, P2-5)
                for future in pending:
                    try:
                        shards.append(future.result())
                    except BaseException:
                        # cancel what has not started, then DRAIN the rest:
                        # every sibling that completes gets its worktree
                        # registered for cleanup; only then re-raise
                        for other in pending:
                            other.cancel()
                        for other in pending:
                            try:
                                shards.append(other.result())
                            except BaseException:
                                pass  # the failing future(s) — already failing
                        raise
            finally:
                pool.shutdown(wait=True)
        # Chunks are contiguous in registry order — concatenation IS the
        # registry-ordered result list, regardless of completion order.
        results = [r for shard_results, _, _ in shards for r in shard_results]
        # Restoration proof: full suite in ONE shard's (restored) worktree —
        # every shard verifies byte-exact restore per mutant, so any shard's
        # final tree is the pristine baseline; shard 0 is used for
        # determinism of the receipt. Failures print their traceback tail AND
        # keep the worktrees for forensics — a restoration failure that
        # cannot be reproduced in a clean copy must be diagnosable from the
        # retained state.
        final = _run(shards[0][2], ["pytest", "-q", "--tb=short", "-p", "no:cacheprovider"], 1800)
        restored_suite_ok = final.returncode == 0
        if not restored_suite_ok:
            print("RESTORATION SUITE FAILURES:", flush=True)
            lines = (final.stdout + final.stderr).splitlines()
            for ln in lines:
                if ln.startswith("FAILED") or ln.startswith("ERROR"):
                    print(" ", ln[:160], flush=True)
            print("---- traceback tail ----", flush=True)
            for ln in lines[-60:]:
                print(" ", ln[:200], flush=True)
            for _, worktree_root, _ in shards:
                print(f"RETAINED WORKTREE: {worktree_root}", flush=True)
            keep_worktrees = True
    finally:
        if not keep_worktrees:
            for _, worktree_root, _ in shards:
                shutil.rmtree(worktree_root, ignore_errors=True)

    counts: dict[str, int] = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    # bind the report to the EXACT registry it ran: the digest over the
    # canonical MUTANTS list (a criterion-6 evaluator recomputes this from
    # the live registry and refuses a mismatch — a report from a different
    # registry revision, or a hand-forged one, does not carry it)
    payload = {
        "mutants": results,
        "totals": counts,
        "restoration_suite_passed": restored_suite_ok,
        "total": len(results),
        "registry_digest": registry_digest(),
        "head": args.head,
        # additive (successor packet 2026-09-03): how this campaign ran.
        # criterion 6 requires selection.mode == "full" — an --only or
        # --changed-since run is iteration evidence, never gate authority.
        "jobs": actual_jobs,
        "selection": {
            "mode": selection_mode,
            "ref": selection_ref,
            "count": len(selected),
        },
    }
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2) + "\n")
    print("| mutant | verdict | invariant |")
    print("|---|---|---|")
    for r in results:
        print(f"| {r['id']} | {r['verdict']} | {r['invariant']} |")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        lines = ["| mutant | verdict | invariant |", "|---|---|---|"]
        lines += [f"| {r['id']} | {r['verdict']} | {r['invariant']} |" for r in results]
        lines.append("")
        lines.append(f"totals: {counts}  total={len(results)}")
        lines.append(f"restoration full-suite pass: {restored_suite_ok}")
        args.markdown.write_text("\n".join(lines) + "\n")
    print(f"totals: {counts}  total={len(results)}")
    print(f"restoration full-suite pass: {restored_suite_ok}")
    print(f"jobs: {actual_jobs}  selection: {selection_mode}")
    bad = sum(v for k, v in counts.items() if k != "KILLED")
    if bad or not restored_suite_ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
