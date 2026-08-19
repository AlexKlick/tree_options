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
        anchor="head.cost_basis = (head.cost_basis - removed).quantize(FEE_TICK)",
        replacement="head.cost_basis = head.cost_basis",
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
        anchor="if as_of is not None and m.available_at > as_of:",
        replacement="if False:",
        selectors=[f"{U}/test_leakage_v2.py"],
        invariant="point-in-time security master hides future mappings",
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
        anchor="if bar.available_at <= decision_at",
        replacement="if True",
        selectors=[f"{U}/test_data_authority.py"],
        invariant="M1-C the authority never returns a bar published after decision_at (future data is invisible at the read gate)",
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
    try:
        shutil.copytree(
            REPO,
            worktree / "repo",
            ignore=shutil.ignore_patterns(".venv", "__pycache__", ".git", "*.pyc", ".pytest_cache"),
        )
        wt = worktree / "repo"
        subprocess.run(
            ["uv", "sync", "--frozen"], cwd=wt, capture_output=True, timeout=600, check=True
        )
        results = [run_mutant(wt, m) for m in MUTANTS]
        # Restoration proof: full suite in the (restored) worktree.
        final = _run(wt, ["pytest", "-q", "--tb=no"], 900)
        restored_suite_ok = final.returncode == 0
        if not restored_suite_ok:
            print("RESTORATION SUITE FAILURES:", flush=True)
            for ln in (final.stdout + final.stderr).splitlines():
                if ln.startswith("FAILED") or ln.startswith("ERROR"):
                    print(" ", ln[:160], flush=True)
    finally:
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
