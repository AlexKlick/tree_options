#!/usr/bin/env python
"""Deterministic mutation harness for the M0 gate (audit §6 contract).

Taxonomy (gate requires zero of everything except KILLED):
  KILLED           owning selectors FAILED behaviorally (pytest "FAILED" lines)
  SURVIVED         owning selectors passed under the mutant
  INVALID_MUTANT   mutant broke compilation, or pytest could not even collect
  TIMEOUT          owning selectors exceeded the per-mutant budget
  MUTATION_DRIFT   anchor not present exactly once (re-pin, never skip)
  HARNESS_ERROR    baseline failure, restore failure, unexpected crash

Runs in a DISPOSABLE WORKTREE copy of the repo — the authoring tree is never
mutated. Baseline selectors must pass before each mutant. The mutated file's
pre-hash is recorded; restoration is byte-verified. After all mutants, the
full suite runs in the worktree to prove restoration. Outputs JSON and
Markdown tables.
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
            "    view = read_ledger(ledger_root)\n"
            "    _check_authority(view, identity)\n\n"
            "    consumption_record = LedgerRecord("
        ),
        replacement=(
            "    view = read_ledger(ledger_root)\n\n    consumption_record = LedgerRecord("
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
            "            for session in fold.test_sessions\n"
            "            if session.isoformat() in _SEALED_HOLDOUT_SESSIONS"
        ),
        replacement=(
            "            for session in fold.test_sessions\n"
            "            if session.isoformat() not in _SEALED_HOLDOUT_SESSIONS"
        ),
        selectors=[f"{U}/test_trials_options_run.py"],
        invariant=(
            "w5 inverting the intersection refuses EVERY trial (the offender"
            " set becomes the UNSEALED sessions): a seal the grid never"
            " touches must never block a legitimate run"
        ),
    ),
    dict(
        id="M270-w5-full-window-overlap-only",
        owner="test_sealed_test_sessions_are_refused_before_registration",
        file="src/tree_options/trials/options_run.py",
        anchor=(
            "            for session in fold.test_sessions\n"
            "            if session.isoformat() in _SEALED_HOLDOUT_SESSIONS"
        ),
        replacement=(
            "            for session in fold.test_sessions\n"
            "            if all(s.isoformat() in _SEALED_HOLDOUT_SESSIONS for s in fold.test_sessions)"
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
        owner="test_repo_yaml_hashes_to_the_ledger_bound_identity",
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
            " untouched yaml (3b0b8a85…) and breaks the ledger-bound pin"
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
            '        "applied": f"unconditional-refusal (declared scope: {FINAL_HOLDOUT_SCOPE})",'
        ),
        replacement='        "applied": "",',
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
            "        if seam_attr is not None and (\n"
            '            hasattr(seam_attr, "__set__") or'
            ' hasattr(seam_attr, "__delete__")\n'
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
            " arm of the same scan keeps its own refusal test)"
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
]

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


def run_mutant(worktree: Path, mutant: dict) -> dict:
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
    base = None
    import time

    for _attempt in range(3):
        try:
            base = _run(
                worktree, ["pytest", *mutant["selectors"], "-q", "-p", "no:cacheprovider"], 600
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None)
    args = parser.parse_args()

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
    worktree = Path(tempfile.mkdtemp(prefix=f"{REPO.name}-mutate-", dir=scratch_parent))
    keep_worktree = False
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
        results = [run_mutant(wt, m) for m in MUTANTS]
        # Restoration proof: full suite in the (restored) worktree. Failures
        # print their traceback tail AND keep the worktree for forensics —
        # a restoration failure that cannot be reproduced in a clean copy
        # (seen twice on test_run_options_trial_end_to_end, clean in both
        # isolated replicas) must be diagnosable from the retained state.
        final = _run(wt, ["pytest", "-q", "--tb=short", "-p", "no:cacheprovider"], 1800)
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
            print(f"RETAINED WORKTREE: {wt}", flush=True)
            keep_worktree = True
    finally:
        if not keep_worktree:
            shutil.rmtree(worktree, ignore_errors=True)

    counts: dict[str, int] = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    payload = {
        "mutants": results,
        "totals": counts,
        "restoration_suite_passed": restored_suite_ok,
        "total": len(results),
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
    bad = sum(v for k, v in counts.items() if k != "KILLED")
    if bad or not restored_suite_ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
