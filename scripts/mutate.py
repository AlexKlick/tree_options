#!/usr/bin/env python
"""Deterministic mutation harness for the M0 gate.

Fixed enumerated mutant list (no mutmut): each mutant names a file, an anchor
string that MUST occur exactly once (MUTATION_DRIFT = red gate: the source
moved and the harness must be re-pinned, never silently skipped), a
replacement, and the pytest selectors that own the invariant. A mutant is
KILLED iff its selectors fail. The file is restored and byte-verified after
every mutant. Exit 0 iff every mutant is KILLED.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

G = "tests/guards"
U = "tests/unit"
P = "tests/properties"

MUTANTS = [
    dict(
        id="M01-availability-boundary",
        file="src/tree_options/guards/availability.py",
        anchor="if ev.available_at <= decision_at:",
        replacement="if ev.available_at < decision_at:",
        selectors=[f"{G}/test_fixture_filings.py"],
        invariant="INV-03/04 inclusive at-close availability",
    ),
    dict(
        id="M02-availability-gutted",
        file="src/tree_options/guards/availability.py",
        anchor="if ev.available_at <= decision_at:",
        replacement="if True:",
        selectors=[f"{G}/test_fixture_filings.py"],
        invariant="INV-03/04 future data rejected",
    ),
    dict(
        id="M03-same-close-instant-level",
        file="src/tree_options/guards/fills.py",
        anchor="execution_at > order.decision_at and",
        replacement="True and",
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="INV-10 instant-level same-close",
    ),
    dict(
        id="M04-same-close-ordinal-level",
        file="src/tree_options/guards/fills.py",
        anchor="exec_ord > decision_ord",
        replacement="True",
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="INV-10 ordinal-level same-close",
    ),
    dict(
        id="M05-partial-fill-side-inverted",
        file="src/tree_options/guards/fills.py",
        anchor='side_size = tq.ask_size if order.side == "buy" else tq.bid_size',
        replacement='side_size = tq.bid_size if order.side == "buy" else tq.ask_size',
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="INV-11 fill size from the correct side",
    ),
    dict(
        id="M06-quote-age-gutted",
        file="src/tree_options/schemas/market.py",
        anchor="if age > max_quote_age_seconds:",
        replacement="if False:",
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="INV-11 stale quote rejected",
    ),
    dict(
        id="M07-received-after-exec-gutted",
        file="src/tree_options/schemas/market.py",
        anchor="if q.received_timestamp > execution_at:",
        replacement="if False:",
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="INV-11 impossible quote ordering rejected",
    ),
    dict(
        id="M08-crossed-quote-gutted",
        file="src/tree_options/schemas/market.py",
        anchor='if not (Decimal("0") < q.bid <= q.ask):\n            raise CrossedQuoteError',
        replacement='if False:\n            raise CrossedQuoteError',
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="INV-11 crossed quote rejected",
    ),
    dict(
        id="M09-dte-filter-gutted",
        file="src/tree_options/candidates/filters.py",
        anchor="if not (self.dte_min <= dte <= self.dte_max):",
        replacement="if False:",
        selectors=[f"{U}/test_candidate_filters.py"],
        invariant="S9.2 DTE band enforced",
    ),
    dict(
        id="M10-budget-cap-off-by-one",
        file="src/tree_options/registry/budget.py",
        anchor="if registry.count_scope(scope_key) >= self.cap:",
        replacement="if registry.count_scope(scope_key) > self.cap:",
        selectors=[f"{U}/test_registry.py"],
        invariant="INV-13 32-cap exact",
    ),
    dict(
        id="M11-duplicate-trial-accepted",
        file="src/tree_options/registry/sqlite.py",
        anchor='"INSERT INTO trials (trial_id, scope_key, hypothesis,"',
        replacement='"INSERT OR IGNORE INTO trials (trial_id, scope_key, hypothesis,"',
        selectors=[f"{U}/test_registry.py"],
        invariant="INV-13 duplicate id rejected",
    ),
    dict(
        id="M12-cash-sign-flipped",
        file="src/tree_options/schemas/trading.py",
        anchor='sign = -1 if self.side == "buy" else 1',
        replacement='sign = 1 if self.side == "buy" else -1',
        selectors=[f"{P}/test_ledger_properties.py"],
        invariant="INV-12 signed cash direction",
    ),
    dict(
        id="M13-fees-zeroed",
        file="src/tree_options/ledger/fees.py",
        anchor="return max(raw, self.minimum_per_order).quantize(FEE_TICK)",
        replacement='return Decimal("0")',
        selectors=[f"{G}/test_fill_engine.py", f"{U}/test_candidate_filters.py"],
        invariant="INV-12 fees charged",
    ),
    dict(
        id="M14-embargo-checker-gutted",
        file="src/tree_options/splitting/checks.py",
        anchor="if first_eval - last_train <= gap:",
        replacement="if False:",
        selectors=[f"{P}/test_split_properties.py"],
        invariant="INV-06 embargo checked",
    ),
    dict(
        id="M15-anchor-checker-gutted",
        file="src/tree_options/splitting/checks.py",
        anchor="if fold.train_sessions and min(fold.train_sessions) != base:",
        replacement="if False:",
        selectors=[f"{P}/test_split_properties.py"],
        invariant="INV-05 anchored expanding train",
    ),
    dict(
        id="M16-coverage-checker-gutted",
        file="src/tree_options/splitting/checks.py",
        anchor="if t in seen and seen[t] != fold.fold_id:",
        replacement="if False:",
        selectors=[f"{P}/test_split_properties.py"],
        invariant="INV-05 test blocks disjoint",
    ),
    dict(
        id="M17-protocol-hash-constant",
        file="src/tree_options/protocol/loader.py",
        anchor='return hashlib.sha256(canonical_json(protocol).encode("utf-8")).hexdigest()',
        replacement='return "0" * 64',
        selectors=[f"{U}/test_protocol_loader.py"],
        invariant="INV-01 protocol version forks on semantic edit",
    ),
    dict(
        id="M18-calendar-checksum-ignored",
        file="src/tree_options/time/calendar.py",
        anchor="if verify_checksum:",
        replacement="if False:",
        selectors=[f"{U}/test_calendar.py"],
        invariant="calendar fixture tamper fails closed",
    ),
    dict(
        id="M19-naive-timestamp-accepted",
        file="src/tree_options/schemas/common.py",
        anchor="if v.tzinfo is None:",
        replacement="if False:",
        selectors=[f"{U}/test_schemas.py"],
        invariant="naive datetimes rejected everywhere",
    ),
    dict(
        id="M20-non-session-fill-accepted",
        file="src/tree_options/guards/fills.py",
        anchor="if not self.calendar.is_session(execution_session):",
        replacement="if False:",
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="fills only on real sessions",
    ),
]


def run_mutant(mutant: dict) -> tuple[str, str]:
    path = REPO / mutant["file"]
    original = path.read_text()
    count = original.count(mutant["anchor"])
    if count != 1:
        return "MUTATION_DRIFT", f"anchor occurs {count}x (expected 1)"
    path.write_text(original.replace(mutant["anchor"], mutant["replacement"]))
    try:
        proc = subprocess.run(
            ["uv", "run", "pytest", *mutant["selectors"], "-q", "-p", "no:cacheprovider"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=600,
        )
        verdict = "KILLED" if proc.returncode != 0 else "SURVIVED"
        tail = (proc.stdout or proc.stderr).strip().splitlines()
        detail = next((ln for ln in reversed(tail) if ln.strip()), "")
    finally:
        path.write_text(original)
        if path.read_text() != original:
            return "RESTORE_FAILED", "file not byte-identical after restore"
    return verdict, detail[:100]


def main() -> int:
    pytest_backup = REPO / ".mutate-backup"
    rows: list[tuple[str, str, str]] = []
    failures = 0
    drift = 0
    for mutant in MUTANTS:
        verdict, detail = run_mutant(mutant)
        rows.append((mutant["id"], verdict, detail))
        if verdict == "SURVIVED":
            failures += 1
        elif verdict != "KILLED":
            drift += 1
        print(f"{mutant['id']:<32} {verdict:<15} {detail}", flush=True)
    if pytest_backup.exists():
        shutil.rmtree(pytest_backup)

    print()
    print("| mutant | verdict | owner |")
    print("|---|---|---|")
    for m, (mid, verdict, _detail) in zip(MUTANTS, rows, strict=True):
        print(f"| {mid} | {verdict} | {m['invariant']} |")
    killed = sum(1 for _, v, _ in rows if v == "KILLED")
    print()
    print(f"total={len(MUTANTS)} killed={killed} survived={failures} drift={drift}")
    if failures or drift:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
