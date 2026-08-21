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
        anchor="if q.received_timestamp > execution_at:",
        replacement="if False:",
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
]

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

    worktree = Path(tempfile.mkdtemp(prefix="tree-options-mutate-"))
    keep_worktree = False
    try:
        shutil.copytree(
            REPO,
            worktree / "repo",
            ignore=shutil.ignore_patterns(".venv", "__pycache__", ".git", "*.pyc", ".pytest_cache"),
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
