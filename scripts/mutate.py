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
        id="M03-same-close-instant",
        file="src/tree_options/guards/fills.py",
        anchor="effective_at > order.decision_at and exec_ord > decision_ord",
        replacement="True and exec_ord > decision_ord",
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="INV-10 instant-level same-close",
    ),
    dict(
        id="M04-same-close-ordinal",
        file="src/tree_options/guards/fills.py",
        anchor="effective_at > order.decision_at and exec_ord > decision_ord",
        replacement="effective_at > order.decision_at and True",
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="INV-10 ordinal-level same-close",
    ),
    dict(
        id="M05-decision-instant-not-close",
        file="src/tree_options/guards/fills.py",
        anchor="order.decision_at != session_close_instant(order.decision_session)",
        replacement="order.decision_at == order.decision_at",
        selectors=[f"{G}/test_fill_semantics_v2.py"],
        invariant="decision_at pinned to session close",
    ),
    dict(
        id="M06-contract-unknown-at-decision",
        file="src/tree_options/guards/fills.py",
        anchor="if not contract.exists_on(order.decision_session):",
        replacement="if False:",
        selectors=[f"{G}/test_fill_semantics_v2.py"],
        invariant="contract known at decision time",
    ),
    dict(
        id="M07-execution-not-session",
        file="src/tree_options/guards/fills.py",
        anchor="if not self.calendar.is_session(execution_session):",
        replacement="if False:",
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="fills only on real sessions",
    ),
    dict(
        id="M08-execution-instant-mismatch",
        file="src/tree_options/guards/fills.py",
        anchor="if not self.calendar.contains_instant(execution_session, effective_at):",
        replacement="if False:",
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="execution instant inside labeled session",
    ),
    dict(
        id="M09-nonstandard-deliverable-accepted",
        file="src/tree_options/guards/fills.py",
        anchor="if not contract.standard_contract_flag:",
        replacement="if False:",
        selectors=[f"{G}/test_fill_semantics_v2.py"],
        invariant="multiplier never silently 100",
    ),
    dict(
        id="M10-side-size-inverted",
        file="src/tree_options/guards/fills.py",
        anchor='displayed = tq.ask_size if order.side == "buy" else tq.bid_size',
        replacement='displayed = tq.bid_size if order.side == "buy" else tq.ask_size',
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="buy uses ask size, sell bid size",
    ),
    dict(
        id="M11-size-fraction-gutted",
        file="src/tree_options/guards/fills.py",
        anchor="capacity = math.floor(self.fill_size_fraction * displayed)",
        replacement="capacity = displayed",
        selectors=[f"{G}/test_fill_semantics_v2.py"],
        invariant="displayed-size fraction enforced",
    ),
    dict(
        id="M12-unmarketable-limit-gutted",
        file="src/tree_options/guards/fills.py",
        anchor='if order.side == "buy" and order.limit_price < price:',
        replacement="if False:",
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="unmarketable limit rejected",
    ),
    dict(
        id="M13-price-rounding-flipped",
        file="src/tree_options/guards/fills.py",
        anchor="ticks = math.ceil(exact / 2)  # conservative: round the BUY price UP",
        replacement="ticks = math.floor(exact / 2)  # MUTATED",
        selectors=[f"{G}/test_fill_semantics_v2.py"],
        invariant="conservative tick rounding",
    ),
    dict(
        id="M14-quote-age-gutted",
        file="src/tree_options/schemas/market.py",
        anchor="if age > max_quote_age_seconds:",
        replacement="if False:",
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="stale quote rejected",
    ),
    dict(
        id="M15-future-quote-gutted",
        file="src/tree_options/schemas/market.py",
        anchor="if q.received_timestamp > execution_at:",
        replacement="if False:",
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="quote from the future rejected",
    ),
    dict(
        id="M16-crossed-gutted",
        file="src/tree_options/schemas/market.py",
        anchor="if q.bid > q.ask:",
        replacement="if False:",
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="crossed quote rejected",
    ),
    dict(
        id="M17-locked-gutted",
        file="src/tree_options/schemas/market.py",
        anchor="if reject_locked and q.bid == q.ask:",
        replacement="if False:",
        selectors=[f"{G}/test_fill_semantics_v2.py"],
        invariant="locked quote rejected",
    ),
    dict(
        id="M18-nonpositive-gutted",
        file="src/tree_options/schemas/market.py",
        anchor="if q.bid <= 0 or q.ask <= 0:",
        replacement="if False:",
        selectors=[f"{G}/test_fill_semantics_v2.py"],
        invariant="nonpositive side rejected",
    ),
    dict(
        id="M19-quote-selection-reaches-back",
        file="src/tree_options/schemas/market.py",
        anchor="eligible = [q for q in quotes if q.received_timestamp <= execution_at]",
        replacement="eligible = list(quotes)",
        selectors=[f"{G}/test_fill_semantics_v2.py"],
        invariant="quote selection monotone in time",
    ),
    dict(
        id="M20-naive-timestamp-accepted",
        file="src/tree_options/schemas/common.py",
        anchor="if v.tzinfo is None:",
        replacement="if False:",
        selectors=[f"{U}/test_schemas.py"],
        invariant="naive datetimes rejected",
    ),
    dict(
        id="M21-signed-cash-flipped",
        file="src/tree_options/schemas/trading.py",
        anchor='sign = -1 if self.side == "buy" else 1',
        replacement='sign = 1 if self.side == "buy" else -1',
        selectors=[f"{U}/test_schemas.py"],
        invariant="signed cash direction",
    ),
    dict(
        id="M22-fees-zeroed",
        file="src/tree_options/ledger/fees.py",
        anchor="return max(raw, self.minimum_per_order).quantize(FEE_TICK)",
        replacement='return Decimal("0")',
        selectors=[f"{G}/test_fill_engine.py"],
        invariant="fees charged",
    ),
    dict(
        id="M23-duplicate-fill-accepted",
        file="src/tree_options/ledger/book.py",
        anchor="if fill.fill_id in self._applied_fill_ids:",
        replacement="if False:",
        selectors=[f"{P}/test_ledger_properties.py"],
        invariant="duplicate fill rejected",
    ),
    dict(
        id="M24-ledger-underflow-gutted",
        file="src/tree_options/ledger/book.py",
        anchor="if held < fill.quantity:",
        replacement="if False:",
        selectors=[f"{P}/test_ledger_properties.py"],
        invariant="ledger underflow rejected",
    ),
    dict(
        id="M25-independent-oracle-broken",
        file="src/tree_options/ledger/book.py",
        anchor="cash += _primitive_cash(fill) - fill.fees",
        replacement="cash += fill.signed_cash() - fill.fees",
        selectors=[f"{P}/test_ledger_properties.py"],
        invariant="independent replay oracle",
    ),
    dict(
        id="M26-embargo-checker-gutted",
        file="src/tree_options/splitting/checks.py",
        anchor="if first_eval - last_train <= gap:",
        replacement="if False:",
        selectors=[f"{P}/test_split_properties.py"],
        invariant="INV-06 embargo checked",
    ),
    dict(
        id="M27-anchor-checker-gutted",
        file="src/tree_options/splitting/checks.py",
        anchor="if fold.train_sessions and min(fold.train_sessions) != base:",
        replacement="if False:",
        selectors=[f"{P}/test_split_properties.py"],
        invariant="INV-05 anchored train",
    ),
    dict(
        id="M28-coverage-checker-gutted",
        file="src/tree_options/splitting/checks.py",
        anchor="if t in seen and seen[t] != fold.fold_id:",
        replacement="if False:",
        selectors=[f"{P}/test_split_properties.py"],
        invariant="INV-05 test blocks disjoint",
    ),
    dict(
        id="M29-session-grouping-gutted",
        file="src/tree_options/splitting/checks.py",
        anchor="if len(roles) > 1:",
        replacement="if False:",
        selectors=[f"{P}/test_split_properties.py"],
        invariant="INV-05 same-session grouping",
    ),
    dict(
        id="M30-budget-cap-off-by-one",
        file="src/tree_options/registry/budget.py",
        anchor="if registry.count_scope(scope_key) >= self.cap:",
        replacement="if registry.count_scope(scope_key) > self.cap:",
        selectors=[f"{U}/test_registry.py"],
        invariant="INV-13 32-cap exact",
    ),
    dict(
        id="M31-duplicate-trial-accepted",
        file="src/tree_options/registry/sqlite.py",
        anchor='"INSERT INTO trials (trial_id, scope_key, hypothesis,"',
        replacement='"INSERT OR IGNORE INTO trials (trial_id, scope_key, hypothesis,"',
        selectors=[f"{U}/test_registry.py"],
        invariant="INV-13 duplicate id rejected",
    ),
    dict(
        id="M32-transition-guard-gutted",
        file="src/tree_options/registry/sqlite.py",
        anchor="if current not in allowed_from:",
        replacement="if False:",
        selectors=[f"{U}/test_registry.py"],
        invariant="REGISTERED->RUNNING->outcome ordering",
    ),
    dict(
        id="M33-scope-canonicality-gutted",
        file="src/tree_options/registry/sqlite.py",
        anchor="if not TrialScope.is_canonical(record.scope_key):",
        replacement="if False:",
        selectors=[f"{U}/test_registry.py"],
        invariant="scope evasion rejected",
    ),
    dict(
        id="M34-candidate-future-input-accepted",
        file="src/tree_options/candidates/filters.py",
        anchor="elif not snap.abs_delta.available_by(snap.decision_at):",
        replacement="elif False:",
        selectors=[f"{U}/test_candidate_filters.py"],
        invariant="future-available inputs rejected",
    ),
    dict(
        id="M35-candidate-acceptance-gutted",
        file="src/tree_options/candidates/filters.py",
        anchor="accepted = not any(r.status in {FAIL, NOT_EVALUABLE} for r in results)",
        replacement="accepted = True",
        selectors=[f"{U}/test_candidate_filters.py"],
        invariant="FAIL/NOT_EVALUABLE block acceptance",
    ),
    dict(
        id="M36-dte-gutted",
        file="src/tree_options/candidates/filters.py",
        anchor="if self.dte_min <= dte <= self.dte_max:",
        replacement="if True:",
        selectors=[f"{U}/test_candidate_filters.py"],
        invariant="DTE band enforced",
    ),
    dict(
        id="M37-protocol-hash-constant",
        file="src/tree_options/protocol/loader.py",
        anchor='return hashlib.sha256(canonical_json(protocol).encode("utf-8")).hexdigest()',
        replacement='return "0" * 64',
        selectors=[f"{U}/test_protocol_loader.py"],
        invariant="INV-01 protocol forks on semantic edit",
    ),
    dict(
        id="M38-calendar-checksum-ignored",
        file="src/tree_options/time/calendar.py",
        anchor="if verify_checksum:",
        replacement="if False:",
        selectors=[f"{U}/test_calendar.py"],
        invariant="calendar tamper fails closed",
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
        behavioral_fail = any(line.startswith(f) for line in out.splitlines() for f in FAILING)
        if behavioral_fail:
            result["verdict"] = "KILLED"
            result["detail"] = next(ln for ln in out.splitlines() if ln.startswith("FAILED"))[:120]
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
